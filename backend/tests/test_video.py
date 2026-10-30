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
def test_short_edition_closing_card(saved, monkeypatch, tmp_path, count):
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from daybrief import video

    settings, brief = saved
    brief = brief.model_copy(update={"selected_events": brief.selected_events[:count]})
    drawing = MagicMock()
    image = MagicMock()
    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(
        Image=SimpleNamespace(new=lambda *args: image),
        ImageDraw=SimpleNamespace(Draw=lambda *args: drawing),
    ))
    monkeypatch.setattr(video, "font", lambda *args: None)
    monkeypatch.setattr(video, "wrap", lambda draw, text, *args: [text])
    target = tmp_path / "closing.png"
    video.card(settings, brief, count + 1, "Sources below.", target)
    texts = [call.args[1] for call in drawing.text.call_args_list]
    assert "CLOSING" in texts
    assert f"FICTIONAL DEMO   /   {count + 2} OF {count + 2}" in texts
    assert "Thanks for watching" in [call.args[1] for call in drawing.multiline_text.call_args_list]
    image.save.assert_called_once_with(target)
