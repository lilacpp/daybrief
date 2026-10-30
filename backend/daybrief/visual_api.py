"""On-demand visual planning; never regenerates narration or calls image models."""

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from .models import VisualPlan, VisualPlanRequest
from .storage import Storage
from .visuals import (
    VisualResolver,
    asset_path,
    brief_fingerprint,
    build_visual_plan,
    canonical_anchor,
    existing_audio_timing,
)


def visual_router(settings):
    router = APIRouter()
    lock = asyncio.Lock()

    def context(brief_id):
        store = Storage(settings.database_url)
        brief = store.get(brief_id)
        if brief is None:
            raise HTTPException(404, "Brief not found")
        service = VisualResolver(settings, mock=settings.daybrief_mode == "mock")
        anchor = canonical_anchor(settings)
        job, durations = existing_audio_timing(settings, brief)
        plan = store.get_visual_plan(brief_id)
        if plan and plan.brief_fingerprint != brief_fingerprint(brief, anchor, job, durations):
            plan = None
        if plan and any(s.article_image is not None or s.visual_mode != "anchor_fullscreen" for s in plan.scenes):
            plan = None
        return store, brief, service, anchor, job, durations, plan

    @router.post("/api/briefs/{brief_id}/visual-plan", response_model=VisualPlan)
    async def create(brief_id: str, body: VisualPlanRequest):
        try:
            async with asyncio.timeout(180), lock:
                store, brief, service, anchor, job, durations, plan = context(brief_id)
                if plan is None:
                    plan = await build_visual_plan(brief, service, anchor, job, durations)
                    store.save_visual_plan(plan)
                return plan
        except TimeoutError:
            raise HTTPException(504, "Visual planning timed out; completed assets remain cached") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/api/briefs/{brief_id}/visual-plan", response_model=VisualPlan)
    def retrieve(brief_id: str):
        plan = context(brief_id)[-1]
        if plan is None:
            raise HTTPException(404, "Visual plan is missing or stale; create it first")
        return plan

    @router.get("/api/briefs/{brief_id}/visual-scenes/{scene_index}")
    def preview(brief_id: str, scene_index: int):
        from .visual_layout import preview_scene

        _, _, service, _, _, _, plan = context(brief_id)
        if plan is None or not 0 <= scene_index < len(plan.scenes):
            raise HTTPException(404, "Visual scene not found")
        return Response(preview_scene(plan.scenes[scene_index], service.root), media_type="image/jpeg")

    @router.get("/api/briefs/{brief_id}/visuals/{event_id}")
    def image(brief_id: str, event_id: str):
        _, _, service, _, _, _, plan = context(brief_id)
        scene = next((s for s in plan.scenes if s.event_id == event_id), None) if plan else None
        if scene is None:
            raise HTTPException(404, "Visual asset not found")
        relative = scene.anchor_asset.image_path
        return FileResponse(asset_path(service.root, relative), headers={"X-Content-Type-Options": "nosniff"})

    return router
