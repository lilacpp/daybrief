from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from .anchors import AnchorId
from .storage import Storage
from .video import Jobs, preflight, render, split_brief


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_paid: bool = False
    anchor: AnchorId = "cat"


def video_router(settings, pool: ThreadPoolExecutor):
    router = APIRouter()

    def lookup(job_id):
        try:
            UUID(job_id)
        except ValueError:
            raise HTTPException(404, "Video task not found") from None
        job = Jobs(settings.database_url).get(job_id)
        if not job:
            raise HTTPException(404, "Video task not found")
        return job

    def create(brief_id, body):
        brief = Storage(settings.database_url).get(brief_id)
        if not brief:
            raise HTTPException(404, "Brief not found")
        if not body.allow_paid:
            raise HTTPException(409, "Explicit Azure paid-service consent is required")
        if (
            not settings.azure_speech_enabled
            or not settings.azure_speech_key
            or not settings.azure_speech_region
        ):
            raise HTTPException(
                503, "Configure and enable Azure Speech on the backend first"
            )
        try:
            import azure.cognitiveservices.speech  # noqa: F401
            import PIL  # noqa: F401

            split_brief(brief)
            preflight(settings, body.anchor)
        except ImportError:
            raise HTTPException(503, "Install the backend video dependencies") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        jobs = Jobs(settings.database_url)
        job = jobs.create(brief.id, "azure", body.anchor)
        pool.submit(render, job["id"], brief, settings, jobs)
        return job

    @router.post("/api/briefs/{brief_id}/videos", status_code=202)
    def create_video(brief_id: str, body: VideoRequest):
        return create(brief_id, body)

    @router.get("/api/videos/{job_id}")
    def status(job_id: str):
        return lookup(job_id)

    @router.post("/api/videos/{job_id}/retry", status_code=202)
    def retry(job_id: str, body: VideoRequest):
        job = lookup(job_id)
        if job["status"] != "failed":
            raise HTTPException(409, "Only failed tasks can be retried")
        if "anchor" not in body.model_fields_set:
            body = body.model_copy(update={"anchor": job.get("anchor", "cat")})
        return create(job["brief_id"], body)

    @router.get("/api/videos/{job_id}/file")
    def video_file(job_id: str, download: bool = False):
        job = lookup(job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "Video is not ready")
        path = Path(settings.video_output_dir).resolve() / job_id / "video.mp4"
        if not path.is_file():
            raise HTTPException(404, "Video file is missing; create another task")
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"daybrief-{job_id}.mp4",
            content_disposition_type="attachment" if download else "inline",
        )

    return router
