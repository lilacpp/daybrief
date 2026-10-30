"""On-demand visual planning; never regenerates narration or calls image models."""

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from .anchors import ANCHORS, AnchorId, bundled_anchor_path
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

    @router.get("/api/anchors")
    def characters():
        return [{"id": a.id, "name": a.name, "style_id": a.style_id,
                 "image_url": f"/api/anchors/{a.id}/image"} for a in ANCHORS.values()]

    @router.get("/api/anchors/{anchor_id}/image")
    def character_image(anchor_id: AnchorId):
        return FileResponse(bundled_anchor_path(anchor_id), media_type="image/png")

    def context(brief_id, anchor_id="cat"):
        store = Storage(settings.database_url)
        brief = store.get(brief_id)
        if brief is None:
            raise HTTPException(404, "Brief not found")
        service = VisualResolver(settings, mock=settings.daybrief_mode == "mock")
        anchor = canonical_anchor(settings, anchor_id)
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
                store, brief, service, anchor, job, durations, plan = context(brief_id, body.anchor)
                if plan is None:
                    plan = await build_visual_plan(brief, service, anchor, job, durations)
                    store.save_visual_plan(plan)
                return plan
        except TimeoutError:
            raise HTTPException(504, "Visual planning timed out; completed assets remain cached") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/api/briefs/{brief_id}/visual-plan", response_model=VisualPlan)
    def retrieve(brief_id: str, anchor: AnchorId = "cat"):
        plan = context(brief_id, anchor)[-1]
        if plan is None:
            raise HTTPException(404, "Visual plan is missing or stale; create it first")
        return plan

    @router.get("/api/briefs/{brief_id}/visual-scenes/{scene_index}")
    def preview(brief_id: str, scene_index: int, anchor: AnchorId = "cat"):
        from .visual_layout import preview_scene

        _, _, service, _, _, _, plan = context(brief_id, anchor)
        if plan is None or not 0 <= scene_index < len(plan.scenes):
            raise HTTPException(404, "Visual scene not found")
        return Response(preview_scene(plan.scenes[scene_index], service.root), media_type="image/jpeg")

    @router.get("/api/briefs/{brief_id}/visuals/{event_id}")
    def image(brief_id: str, event_id: str, anchor: AnchorId = "cat"):
        _, _, service, _, _, _, plan = context(brief_id, anchor)
        scene = next((s for s in plan.scenes if s.event_id == event_id), None) if plan else None
        if scene is None:
            raise HTTPException(404, "Visual asset not found")
        relative = scene.anchor_asset.image_path
        return FileResponse(asset_path(service.root, relative), headers={"X-Content-Type-Options": "nosniff"})

    return router
