import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from daybrief.adapters import MockModel, MockSearch
from daybrief.anchors import ANCHORS, presentation_lines
from daybrief.api import create_app
from daybrief.config import Settings
from daybrief.models import VisualAsset, VisualPlanRequest
from daybrief.pipeline import run_pipeline
from daybrief.storage import Storage
from daybrief.video import Jobs
from daybrief.video_api import VideoRequest, video_router
from daybrief.visual_layout import preview_scene, scene_ffmpeg_args
from daybrief.visuals import VisualResolver, build_visual_plan, canonical_anchor


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, daybrief_mode="mock", database_url=f"sqlite:///{tmp_path}/brief.db", video_output_dir=str(tmp_path / "videos"))


@pytest.fixture
def brief():
    return asyncio.run(run_pipeline("general", 24, datetime(2026,10,14,12,tzinfo=UTC), MockSearch(), MockModel(), "mock"))


def test_default_and_invalid_anchor():
    assert VideoRequest().anchor == VisualPlanRequest().anchor == "cat"
    for model in [VideoRequest, VisualPlanRequest]:
        for invalid in ["wolf", "../cat", "", None]:
            with pytest.raises(ValidationError):
                model(anchor=invalid)


@pytest.mark.parametrize("anchor_id", ANCHORS)
def test_selected_anchor_in_all_scenes_and_article_layout(settings, brief, anchor_id):
    before = brief.model_dump_json()
    resolver = VisualResolver(settings, mock=True)
    anchor = canonical_anchor(settings, anchor_id)
    plan = asyncio.run(build_visual_plan(brief, resolver, anchor))
    assert all(s.anchor_asset.character_id == anchor_id for s in plan.scenes)
    assert len({s.anchor_asset.image_path for s in plan.scenes}) == 1
    assert all(s.visual_mode == "anchor_fullscreen" for s in plan.scenes)
    assert [s.narration_text for s in plan.scenes[1:-1]] == [s.text for s in brief.narrative_segments]
    # Existing optional article layout still uses the selected character as PiP.
    scene = plan.scenes[1].model_copy(update={
        "visual_mode":"article_with_anchor", "motion":"slow_zoom",
        "article_image":VisualAsset(type="article_image", local_path=anchor.image_path),
    })
    assert preview_scene(scene, resolver.root)
    args = scene_ffmpeg_args(scene, resolver.root, "out.mp4")
    assert str(resolver.root / anchor.image_path) in args
    assert "overlay=928:472" in args[args.index("-filter_complex") + 1]
    assert brief.model_dump_json() == before


def test_anchor_catalog_plan_api_and_cache(settings, brief):
    Storage(settings.database_url).save(brief)
    with TestClient(create_app(settings)) as client:
        assert [a["id"] for a in client.get("/api/anchors").json()] == list(ANCHORS)
        assert client.get("/api/anchors/wolf/image").status_code == 422
        path = f"/api/briefs/{brief.id}/visual-plan"
        for anchor in ANCHORS:
            assert client.get(f"/api/anchors/{anchor}/image").status_code == 200
            response = client.post(path, json={"anchor":anchor})
            assert response.status_code == 200, response.text
            assert all(s["anchor_asset"]["character_id"] == anchor for s in response.json()["scenes"])
            assert client.get(path, params={"anchor":anchor}).json() == response.json()
        assert client.post(path, json={"anchor":"wolf"}).status_code == 422
        assert client.get(path, params={"anchor":"cat"}).status_code == 404
    assert Storage(settings.database_url).get(brief.id) == brief


@pytest.mark.parametrize("sensitivity", ["normal","sensitive","critical"])
@pytest.mark.parametrize("count", [1,2,3])
def test_short_restrained_templates(sensitivity, count):
    lines = presentation_lines([SimpleNamespace(sensitivity=sensitivity)] * count)
    assert all(5 <= len(line.split()) <= 12 for line in lines)
    assert sum(len(line.split()) for line in lines) == 10
    assert lines == presentation_lines([SimpleNamespace(sensitivity=sensitivity)] * count)
    if sensitivity != "normal":
        assert lines == ("Welcome. Here is your DayBrief.", "Thank you for joining us.")


def test_mixed_sensitive_edition_is_restrained():
    assert presentation_lines([SimpleNamespace(sensitivity="normal"), SimpleNamespace(sensitivity="critical")])[0] == "Welcome. Here is your DayBrief."


def test_video_request_persists_choice_and_retry(settings, brief, monkeypatch):
    import sys
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    import daybrief.video_api as module

    Storage(settings.database_url).save(brief)
    settings = settings.model_copy(update={"azure_speech_enabled":True, "azure_speech_key":"test-only", "azure_speech_region":"westus2"})
    monkeypatch.setitem(sys.modules, "azure", MagicMock())
    monkeypatch.setitem(sys.modules, "azure.cognitiveservices", MagicMock())
    monkeypatch.setitem(sys.modules, "azure.cognitiveservices.speech", MagicMock())
    checked = []
    monkeypatch.setattr(module, "preflight", lambda settings, anchor: checked.append(anchor))
    pool = MagicMock()
    app = FastAPI()
    app.include_router(video_router(settings, pool))
    with TestClient(app) as client:
        response = client.post(f"/api/briefs/{brief.id}/videos", json={"allow_paid":True,"anchor":"fox"})
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["anchor"] == "fox"
        assert pool.submit.call_args.args[-1].get(job["id"])["anchor"] == "fox"
        job["status"] = "failed"
        Jobs(settings.database_url).save(job)
        retry = client.post(f"/api/videos/{job['id']}/retry", json={"allow_paid":True})
        assert retry.json()["anchor"] == "fox"
        assert checked == ["fox", "fox"]
