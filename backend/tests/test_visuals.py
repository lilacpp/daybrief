import asyncio
import io
import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from daybrief.adapters import MockModel, MockSearch
from daybrief.api import create_app
from daybrief.config import Settings
from daybrief.models import VisualAsset
from daybrief.pipeline import run_pipeline
from daybrief.storage import Storage
from daybrief.video import Jobs
from daybrief.visual_http import Fetched, PublicWeb, VisualFetchError, public_url
from daybrief.visual_images import article_candidates, validated_image
from daybrief.visual_layout import crossfade_filter, preview_scene, scene_ffmpeg_args
from daybrief.visuals import VisualResolver, build_visual_plan, existing_audio_timing


@pytest.fixture
def settings(tmp_path):
    return Settings(daybrief_mode="mock", database_url=f"sqlite:///{tmp_path}/brief.db", video_output_dir=str(tmp_path / "videos"))


@pytest.fixture
def brief():
    return asyncio.run(run_pipeline("general", 24, datetime(2026, 10, 14, 12, tzinfo=UTC), MockSearch(), MockModel(), "mock"))


def raster(size=(1280, 720)):
    out = io.BytesIO()
    Image.new("RGB", size, "#547798").save(out, "PNG")
    return out.getvalue()


class Web:
    def __init__(self, html):
        self.html = html
        self.calls = []

    async def fetch(self, url, limit):
        self.calls.append(url)
        if url.endswith("/story"):
            return Fetched(url, 200, {"content-type": "text/html"}, self.html.encode())
        return Fetched(url, 200, {"content-type": "image/png"}, raster())


def test_metadata_priority_credit_and_body_scope():
    html = '''<img src="/outside.png"><meta property="og:image" content="/news.png">
    <meta name="twitter:image" content="/twitter.png"><article><img src="/body.png"></article>
    <script type="application/ld+json">{"@type":"NewsArticle","image":{"url":"/news.png","creditText":"Photo credit"}}</script>'''
    candidates = article_candidates(html, "https://news.example/story")
    assert [c.url for c in candidates] == ["https://news.example/news.png", "https://news.example/twitter.png", "https://news.example/body.png"]
    assert candidates[0].credit == "Photo credit"


@pytest.mark.parametrize("html", [
    '<meta property="og:image" content="/logo.png">',
    '<meta property="og:image" content="/news.png"><meta name="robots" content="noimageindex">',
    '<script type="application/ld+json">{"@type":"Article","isAccessibleForFree":false,"image":"/news.png"}</script>',
    '<article><img src="/news.png" width="20" height="20"></article>',
])
def test_unsuitable_metadata(html):
    assert article_candidates(html, "https://news.example/story") == []


@pytest.mark.parametrize("url", ["file:///tmp/image", "http://127.0.0.1/x", "http://169.254.169.254/x", "https://user:pass@example.com/x", "http://[::1]/x"])
def test_private_urls_rejected(url):
    with pytest.raises(VisualFetchError):
        public_url(url)


@pytest.mark.parametrize("data,mime", [(b"broken", "image/png"), (raster((20,20)), "image/png"), (raster(), "text/html"), (raster((1600,360)), "image/png")])
def test_invalid_rasters(data, mime):
    with pytest.raises(VisualFetchError):
        validated_image(data, mime)


def test_article_cache_and_attribution(settings):
    web = Web('<meta property="og:image" content="/news.png">')
    resolver = VisualResolver(settings, web=web)
    first = asyncio.run(resolver.resolve_article_image("https://news.example/story", "event", "Publisher"))
    second = asyncio.run(resolver.resolve_article_image("https://news.example/story", "event", "Publisher"))
    assert first == second
    assert first.publisher == "Publisher"
    assert first.source_url == "https://news.example/story"
    assert first.url == "https://news.example/news.png"
    assert web.calls.count(first.url) == 1


def test_modes_order_consistency_and_content(settings, brief):
    original = brief.model_dump_json()
    resolver = VisualResolver(settings, mock=True)
    plan = asyncio.run(build_visual_plan(brief, resolver))
    assert all(s.visual_mode == "anchor_fullscreen" for s in plan.scenes)
    assert len({s.anchor_asset.image_path for s in plan.scenes}) == 1
    assert [s.narration_text for s in plan.scenes] == [brief.opening, *(s.text for s in brief.narrative_segments), brief.closing]
    assert [s.event_id for s in plan.scenes[1:-1]] == [e.id for e in brief.selected_events]
    assert brief.model_dump_json() == original
    assert Image.open(io.BytesIO(preview_scene(plan.scenes[0], resolver.root))).size == (1280,720)


def test_article_available_still_uses_cat_without_fetch(settings, brief):
    resolver = VisualResolver(settings, mock=False)

    async def forbidden(*args, **kwargs):
        pytest.fail("Cat-only planning must never fetch article images")

    resolver.resolve_visual = forbidden
    resolver.web.fetch = forbidden
    plan = asyncio.run(build_visual_plan(brief, resolver))
    assert all(s.visual_mode == "anchor_fullscreen" for s in plan.scenes)
    assert all(s.article_image is None for s in plan.scenes)
    assert len({s.anchor_asset.image_path for s in plan.scenes}) == 1
    assert Image.open(io.BytesIO(preview_scene(plan.scenes[1], resolver.root))).size == (1280,720)


def test_audio_timing(settings, brief):
    jobs = Jobs(settings.database_url)
    job = jobs.create(brief.id, "mock")
    job["status"] = "completed"
    jobs.save(job)
    from pathlib import Path
    root = Path(settings.video_output_dir) / job["id"]
    root.mkdir(parents=True)
    texts = [brief.opening, *(s.text for s in brief.narrative_segments), brief.closing]
    (root / "manifest.json").write_text(json.dumps({"brief_id": brief.id, "scenes": [{"text": t, "duration": 2.5} for t in texts]}))
    job_id, durations = existing_audio_timing(settings, brief)
    assert job_id == job["id"]
    plan = asyncio.run(build_visual_plan(brief, VisualResolver(settings, mock=True), audio_job_id=job_id, durations=durations))
    assert all(s.duration_seconds == 2.5 and s.duration_source == "audio_manifest" for s in plan.scenes)
    (root / "manifest.json").write_text('{}')
    assert existing_audio_timing(settings, brief) == (None,None)


def test_api_persistence(settings, brief):
    store = Storage(settings.database_url)
    store.save(brief)
    with TestClient(create_app(settings)) as client:
        path = f"/api/briefs/{brief.id}/visual-plan"
        response = client.post(path, json={})
        assert response.status_code == 200, response.text
        assert client.get(path).json() == response.json()
        assert client.post(path, json={}).json() == response.json()
        assert client.get(f"/api/briefs/{brief.id}/visual-scenes/0").status_code == 200
    assert store.get(brief.id) == brief


def test_robots_and_redirect_checks(monkeypatch):
    web = PublicWeb(1)
    async def request(url, limit):
        if url.endswith("robots.txt"):
            return Fetched(url, 200, {}, b"User-agent: *\nDisallow: /blocked")
        return Fetched(url, 302, {"location": "http://127.0.0.1/private"}, b"")
    monkeypatch.setattr(web, "_request", request)
    for url in ["https://news.example/blocked", "https://news.example/story"]:
        with pytest.raises(VisualFetchError):
            asyncio.run(web.fetch(url, 100))


def test_ffmpeg_scene_smoke(settings, brief, tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is not installed")
    resolver = VisualResolver(settings, mock=True)
    scene = asyncio.run(build_visual_plan(brief, resolver)).scenes[0]
    scene.duration_seconds = 0.2
    for article in [False, True]:
        if article:
            path = resolver.root / "article.png"
            path.write_bytes(raster())
            scene = scene.model_copy(update={"article_image": VisualAsset(type="article_image", local_path="article.png"), "motion":"gentle_pan", "visual_mode":"article_with_anchor"})
        output = tmp_path / f"{article}.mp4"
        result = subprocess.run(scene_ffmpeg_args(scene, resolver.root, output), capture_output=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr.decode()
        assert output.stat().st_size > 0
    assert crossfade_filter(1) == "xfade=transition=fade:duration=0.25:offset=1"


def test_dns_rejects_mixed_private_answers(monkeypatch):
    from daybrief.visual_http import public_address

    async def run():
        async def answers(*args, **kwargs):
            return [(0,0,0,"",("93.184.216.34",443)), (0,0,0,"",("127.0.0.1",443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", answers)
        with pytest.raises(VisualFetchError):
            await public_address("https://news.example/story")
    asyncio.run(run())


def test_pinned_connection_retains_tls_hostname(monkeypatch):
    import httpx

    import daybrief.visual_http as module

    async def address(url):
        return "93.184.216.34"

    def handle(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "news.example"
        assert request.extensions["sni_hostname"] == "news.example"
        return httpx.Response(200, content=b"ok")

    client = httpx.AsyncClient
    monkeypatch.setattr(module, "public_address", address)
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    assert asyncio.run(PublicWeb(1)._once("https://news.example/story", 10)).body == b"ok"


def test_bad_og_uses_twitter(settings):
    class Fallback(Web):
        async def fetch(self, url, limit):
            if url.endswith("bad.png"):
                return Fetched(url, 200, {}, b"broken")
            return await super().fetch(url, limit)
    web = Fallback('<meta property="og:image" content="/bad.png"><meta name="twitter:image" content="/good.png">')
    result = asyncio.run(VisualResolver(settings, web=web).resolve_article_image("https://news.example/story", "event"))
    assert result.url.endswith("good.png")


def test_source_priority_and_early_stop(settings, brief):
    from daybrief.visuals import ranked_sources, usable_claims
    event = brief.selected_events[0]
    claims = usable_claims(event, brief.narrative_segments[0].supporting_claim_ids)
    expected = ranked_sources(event, claims, brief.sources)
    resolver = VisualResolver(settings)
    called = []
    async def resolve(url, event_id, publisher):
        called.append(url)
        return VisualAsset(type="article_image", url="https://news.example/image.jpg")
    resolver.resolve_article_image = resolve
    asyncio.run(resolver.resolve_visual(event, brief.sources, brief.narrative_segments[0].supporting_claim_ids))
    assert called == [expected[0][0]]


def test_cached_article_plan_is_not_served(settings, brief):
    store = Storage(settings.database_url)
    store.save(brief)
    plan = asyncio.run(build_visual_plan(brief, VisualResolver(settings, mock=True)))
    plan.scenes[1] = plan.scenes[1].model_copy(update={
        "visual_mode": "article_with_anchor",
        "article_image": VisualAsset(type="article_image", local_path="old.jpg"),
        "motion": "slow_zoom",
    })
    store.save_visual_plan(plan)
    with TestClient(create_app(settings)) as client:
        path = f"/api/briefs/{brief.id}/visual-plan"
        assert client.get(path).status_code == 404
        response = client.post(path, json={})
        assert response.status_code == 200
        assert all(s["visual_mode"] == "anchor_fullscreen" and s["article_image"] is None for s in response.json()["scenes"])
