import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daybrief.adapters import MockModel, MockSearch
from daybrief.api import create_app
from daybrief.config import Settings
from daybrief.pipeline import run_pipeline
from daybrief.storage import Storage
from daybrief.video import Jobs, captions, render, split_brief, synthesize


@pytest.fixture
def saved(tmp_path):
    settings = Settings(
        daybrief_mode="mock",
        database_url=f"sqlite:///{tmp_path}/briefs.db",
        video_output_dir=str(tmp_path / "videos"),
    )
    brief = asyncio.run(
        run_pipeline(
            "general", 24, datetime.now(UTC), MockSearch(), MockModel(), "mock"
        )
    )
    Storage(settings.database_url).save(brief)
    return settings, brief


def test_split_preserves_exact_saved_narration(saved):
    _, brief = saved
    assert "\n\n".join(split_brief(brief)) == brief.narration
    altered = brief.model_copy(
        update={"narration": brief.narration.replace("\n\n", "\n", 1)}
    )
    with pytest.raises(ValueError):
        split_brief(altered)
    parts = split_brief(brief)
    parts[1] += " An invented fact."
    with pytest.raises(ValueError, match="verified claims"):
        split_brief(brief.model_copy(update={"narration": "\n\n".join(parts)}))


def test_tts_captions_follow_audio_offsets():
    text = "One two three four five six seven eight nine."
    points = [
        {"offset": text.index(word), "start": i * 0.4, "length": len(word)}
        for i, word in enumerate(text.split())
    ]
    cues = captions(text, 4.5, points)
    assert cues[0][1] == pytest.approx(2.8)
    assert cues[-1][1] == 4.5
    assert " ".join(c[2] for c in cues) == text
    assert captions(text, 8, [])[-1][1] == 8


def test_paid_calls_disabled_by_default(saved, tmp_path):
    settings, _ = saved
    with pytest.raises(ValueError, match="disabled"):
        synthesize("Hello", tmp_path / "audio.wav", settings, "azure")


def test_api_requires_consent_and_backend_enable(saved):
    settings, brief = saved
    with TestClient(create_app(settings)) as client:
        url = f"/api/briefs/{brief.id}/videos"
        assert client.post(url, json={}).status_code == 409
        assert client.post(url, json={"allow_paid": True}).status_code == 503
        assert client.get("/api/videos/invalid").status_code == 404
        assert (
            client.get(f"/api/briefs/{brief.id}").json()["narration"] == brief.narration
        )


def test_failed_video_retains_brief_and_error_stage(saved, monkeypatch):
    monkeypatch.setattr("daybrief.video.preflight", lambda settings: None)
    settings, brief = saved
    jobs = Jobs(settings.database_url)
    job = jobs.create(brief.id, "azure")
    render(job["id"], brief, settings, jobs)
    failed = jobs.get(job["id"])
    assert failed["status"] == "failed"
    assert failed["stage"] == "speech_1"
    assert Storage(settings.database_url).get(brief.id).narration == brief.narration


def test_restart_marks_incomplete_jobs_retryable(saved):
    settings, brief = saved
    jobs = Jobs(settings.database_url)
    job = jobs.create(brief.id, "azure")
    jobs.recover()
    assert jobs.get(job["id"])["status"] == "failed"
    assert "interrupted" in jobs.get(job["id"])["error"]


def test_completed_file_served_and_incomplete_hidden(saved):
    settings, brief = saved
    with TestClient(create_app(settings)) as client:
        jobs = Jobs(settings.database_url)
        job = jobs.create(brief.id, "azure")
        assert client.get(f"/api/videos/{job['id']}/file").status_code == 409
        root = Path(settings.video_output_dir) / job["id"]
        root.mkdir(parents=True)
        (root / "video.mp4").write_bytes(b"test-video")
        job.update(status="completed")
        jobs.save(job)
        response = client.get(f"/api/videos/{job['id']}/file?download=true")
        assert response.content == b"test-video"
        assert "attachment" in response.headers["content-disposition"]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_renderer_measures_audio_and_caches_segments(saved, monkeypatch, count):
    """Orchestration test; does not claim to validate actual encoding."""
    from daybrief import video

    settings, brief = saved
    from daybrief.pipeline import generate_brief

    brief = asyncio.run(generate_brief(
        brief.selected_events[:count], brief.sources, brief.topic, 24,
        brief.generated_at, MockModel(), "mock",
    ))
    calls = []

    def speech(text, path, settings, provider):
        calls.append(text)
        path.write_bytes(b"fake-wave")
        return []

    monkeypatch.setattr(video, "preflight", lambda settings: None)
    monkeypatch.setattr(video, "synthesize", speech)
    monkeypatch.setattr(
        video, "duration", lambda p: (count + 2) * 10 if p.name == "video.mp4" else 10
    )
    monkeypatch.setattr(video, "card", lambda *args: None)
    monkeypatch.setattr(video, "mouth_timing", lambda audio, seconds: [(0, seconds, 0)])
    monkeypatch.setattr(video, "command", lambda *args: None)
    jobs = Jobs(settings.database_url)
    for _ in range(2):
        job = jobs.create(brief.id, "azure")
        render(job["id"], brief, settings, jobs)
        assert jobs.get(job["id"])["status"] == "completed"
    manifest = Path(settings.video_output_dir) / job["id"] / "scenes.txt"
    assert len(manifest.read_text().splitlines()) == count + 2
    assert calls == split_brief(brief)


@pytest.mark.parametrize("count", [1, 2])
def test_short_edition_closing_card(saved, tmp_path, count):
    from PIL import Image, ImageOps

    from daybrief import video
    from daybrief.visuals import asset_path, canonical_anchor, visual_root

    settings, brief = saved
    brief = brief.model_copy(update={"selected_events": brief.selected_events[:count]})
    target = tmp_path / "closing.png"
    video.card(settings, brief, count + 1, "Sources below.", target)
    anchor = canonical_anchor(settings)
    with Image.open(asset_path(visual_root(settings), anchor.image_path)) as source:
        expected = ImageOps.fit(source.convert("RGB"), (1280, 720))
    with Image.open(target) as actual:
        assert actual.size == (1280, 720)
        # The unobscured presenter is the canonical cat, not the old title card.
        assert actual.crop((200, 80, 1080, 500)).tobytes() == expected.crop((200, 80, 1080, 500)).tobytes()


def test_actual_cat_video_encoding(saved, monkeypatch, tmp_path):
    import shutil
    import wave

    from daybrief import video
    from daybrief.pipeline import generate_brief

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not installed")
    settings, brief = saved
    brief = asyncio.run(generate_brief(
        brief.selected_events[:1], brief.sources, brief.topic, 24,
        brief.generated_at, MockModel(), "mock",
    ))

    def synthetic_speech(text, path, settings, provider):
        import math
        import struct

        samples = [0] * 4000 + [int(9000 * math.sin(i * 0.2)) for i in range(4000)] + [0] * 4000
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(24000)
            out.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        return []

    monkeypatch.setattr(video, "synthesize", synthetic_speech)
    jobs = Jobs(settings.database_url)
    job = jobs.create(brief.id, "azure")
    video.render(job["id"], brief, settings, jobs)
    assert jobs.get(job["id"])["status"] == "completed", jobs.get(job["id"])
    root = Path(settings.video_output_dir) / job["id"]
    assert (root / "video.mp4").stat().st_size > 0
    import json
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["visual_style"] == "cat-only-v1"
    assert manifest["mouth_animation"] == "audio_energy_v1"
    frames = (root / "0.txt").read_text()
    assert "0-0-0.png" in frames and "0-0-2.png" in frames
    assert [s["text"] for s in manifest["scenes"]] == video.split_brief(brief)
    assert (manifest["width"], manifest["height"]) == (1280, 720)


def test_preflight_does_not_write_runtime_assets(saved, monkeypatch, tmp_path):
    from daybrief import video, visuals

    settings, _ = saved
    # A file cannot contain a directory: any attempted cache write would fail,
    # regardless of the permissions of the user running this test.
    blocked = tmp_path / "read-only-app"
    blocked.write_text("not a writable directory")
    settings = settings.model_copy(update={"video_output_dir": str(blocked / "videos")})
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(video, "font", lambda *args: None)
    def forbidden(*args, **kwargs):
        pytest.fail("Preflight must not write an anchor cache")
    monkeypatch.setattr(visuals, "atomic_write", forbidden)
    video.preflight(settings)
    assert blocked.read_text() == "not a writable directory"


def test_preflight_still_validates_anchor(saved, monkeypatch):
    from daybrief import video, visuals

    settings, _ = saved
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(video, "font", lambda *args: None)
    def invalid(*args, **kwargs):
        raise ValueError("Invalid anchor image")
    monkeypatch.setattr(visuals, "validated_image", invalid)
    with pytest.raises(ValueError, match="Invalid anchor image"):
        video.preflight(settings)


def test_mouth_motion_follows_audio_and_closes_for_silence(tmp_path):
    import math
    import struct
    import wave

    from daybrief.video import mouth_timing

    path = tmp_path / "speech.wav"
    samples = [0] * 12000 + [int(9000 * math.sin(i * 0.2)) for i in range(12000)] + [0] * 12000
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    poses = mouth_timing(path, 1.5)
    assert all(pose == 0 for start, end, pose in poses if end <= 0.5 or start >= 1)
    assert any(pose > 0 for start, end, pose in poses if 0.5 <= start < 1)
    assert poses[-1][1] == 1.5


def test_talking_pose_changes_only_mouth(saved, tmp_path):
    from PIL import Image, ImageChops

    from daybrief.video import card

    settings, brief = saved
    closed, opened = tmp_path / "closed.png", tmp_path / "opened.png"
    card(settings, brief, 0, "Hello.", closed)
    card(settings, brief, 0, "Hello.", opened, mouth_open=2)
    with Image.open(closed) as a, Image.open(opened) as b:
        box = ImageChops.difference(a, b).getbbox()
        assert box is not None
        assert box[0] >= 615 and box[1] >= 260 and box[2] <= 638 and box[3] <= 276
