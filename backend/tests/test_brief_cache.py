import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from daybrief import api
from daybrief.adapters import MockModel, MockSearch, ModelOutputError, ProviderError
from daybrief.brief_cache import brief_cache_key, usable_cached_brief
from daybrief.config import Settings
from daybrief.pipeline import generate_brief, run_pipeline
from daybrief.storage import Storage

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


@pytest.fixture
def base():
    return asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), MockModel(), "mock"))


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, daybrief_mode="mock", database_url=f"sqlite:///{tmp_path}/brief.db",
                    video_output_dir=str(tmp_path / "videos"))


def harness(monkeypatch, settings, base, count=2):
    clock = [NOW]
    calls = []
    monkeypatch.setattr(api, "utc_now", lambda: clock[0])
    async def pipeline(topic, hours, now, search, model, mode, **kwargs):
        calls.append((topic, hours, mode))
        return await generate_brief(base.selected_events[:count], base.sources, topic, hours, now, MockModel(), mode)
    monkeypatch.setattr(api, "run_pipeline", pipeline)
    return TestClient(api.create_app(settings)), calls, clock


def test_defaults_and_env(monkeypatch):
    monkeypatch.delenv("DAYBRIEF_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("DAYBRIEF_CACHE_TTL_SECONDS", raising=False)
    assert Settings(_env_file=None).daybrief_cache_enabled is True
    assert Settings(_env_file=None).daybrief_cache_ttl_seconds == 1800
    monkeypatch.setenv("DAYBRIEF_CACHE_ENABLED", "false")
    monkeypatch.setenv("DAYBRIEF_CACHE_TTL_SECONDS", "42")
    assert Settings(_env_file=None).daybrief_cache_enabled is False
    assert Settings(_env_file=None).daybrief_cache_ttl_seconds == 42


@pytest.mark.parametrize("ttl", [0, -1, "invalid", "1.5", 1.5])
def test_invalid_ttl_rejected(ttl):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, daybrief_cache_ttl_seconds=ttl)


@pytest.mark.parametrize("count", [2, 3])
def test_usable_brief_cached_and_pipeline_skipped(monkeypatch, settings, base, count, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    client, calls, clock = harness(monkeypatch, settings, base, count)
    with client:
        first = client.post("/api/briefs/generate", json={"topic":"general"})
        assert first.status_code == 200, first.text
        clock[0] += timedelta(minutes=29)
        second = client.post("/api/briefs/generate", json={"topic":"general", "lookback_hours":24})
    assert len(calls) == 1
    assert not first.json()["is_cached"]
    assert second.json()["is_cached"]
    assert second.json()["story_count"] == count
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["generated_at"] == first.json()["generated_at"]
    assert datetime.fromisoformat(second.json()["expires_at"]) == NOW + timedelta(seconds=1800)
    for event in ["brief_cache_hit", "brief_cache_miss", "brief_cache_saved"]:
        assert event in caplog.text


def test_one_story_persisted_but_not_cached(monkeypatch, settings, base):
    client, calls, _ = harness(monkeypatch, settings, base, 1)
    with client:
        first = client.post("/api/briefs/generate", json={"topic":"general"}).json()
        second = client.post("/api/briefs/generate", json={"topic":"general"}).json()
        assert client.get(f"/api/briefs/{first['id']}").status_code == 200
    assert len(calls) == 2
    assert first["id"] != second["id"]
    assert first["expires_at"] is None
    assert first["story_count"] == 1
    assert not second["is_cached"]


@pytest.mark.parametrize("seconds", [1800, 1801])
def test_expiry_boundary_reruns_pipeline(monkeypatch, settings, base, seconds, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    client, calls, clock = harness(monkeypatch, settings, base)
    with client:
        first = client.post("/api/briefs/generate", json={"topic":"general"}).json()
        clock[0] += timedelta(seconds=seconds)
        second = client.post("/api/briefs/generate", json={"topic":"general"}).json()
    assert len(calls) == 2 and first["id"] != second["id"]
    assert not second["is_cached"]
    assert "brief_cache_expired" in caplog.text


def test_disabled_bypasses_all_cache_operations(monkeypatch, settings, base):
    settings.daybrief_cache_enabled = False
    def forbidden(*args):
        pytest.fail("Disabled cache must not read or write cache entries")
    monkeypatch.setattr(Storage, "get_cached_brief", forbidden)
    monkeypatch.setattr(Storage, "save_cached_brief", forbidden)
    client, calls, _ = harness(monkeypatch, settings, base)
    with client:
        for _ in range(2):
            response = client.post("/api/briefs/generate", json={"topic":"general"})
            assert response.status_code == 200
            assert response.json()["expires_at"] is None
            assert not response.json()["is_cached"]
    assert len(calls) == 2
    with sqlite3.connect(Storage(settings.database_url).path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='brief_cache'").fetchone() is None
        assert db.execute("SELECT count(*) FROM briefs").fetchone()[0] == 2


def test_request_content_and_mode_isolation(monkeypatch, settings, base):
    client, calls, _ = harness(monkeypatch, settings, base)
    requests = [{"topic":"general", "lookback_hours":24},
                {"topic":"science", "lookback_hours":24},
                {"topic":"general", "lookback_hours":48}]
    with client:
        ids = [client.post("/api/briefs/generate", json=r).json()["id"] for r in requests]
        assert len(set(ids)) == 3
        for r, ident in zip(requests, ids, strict=True):
            response = client.post("/api/briefs/generate", json=r).json()
            assert response["id"] == ident and response["is_cached"]
    assert len(calls) == 3
    assert brief_cache_key("general",24,"mock") != brief_cache_key("general",24,"real")


def test_anchor_selection_does_not_invalidate_brief(monkeypatch, settings, base):
    client, calls, _ = harness(monkeypatch, settings, base)
    with client:
        brief = client.post("/api/briefs/generate", json={"topic":"general"}).json()
        for anchor in ["cat", "dog"]:
            plan = client.post(f"/api/briefs/{brief['id']}/visual-plan", json={"anchor":anchor})
            assert plan.status_code == 200, plan.text
            response = client.post("/api/briefs/generate", json={"topic":"general"}).json()
            assert response["id"] == brief["id"] and response["is_cached"]
    assert len(calls) == 1
    assert "anchor" not in json.loads(brief_cache_key("general",24,"mock"))["content"]


@pytest.mark.parametrize("failure", [ProviderError("provider failed"), ModelOutputError("schema failed"), ProviderError("narrative_grounding_failed")])
def test_failures_are_never_cached(monkeypatch, settings, failure):
    calls = []
    async def fail(*args, **kwargs):
        calls.append(1)
        raise failure
    monkeypatch.setattr(api, "run_pipeline", fail)
    with TestClient(api.create_app(settings)) as client:
        for _ in range(2):
            assert client.post("/api/briefs/generate", json={"topic":"general"}).status_code == 502
    assert len(calls) == 2
    with sqlite3.connect(Storage(settings.database_url).path) as db:
        assert db.execute("SELECT count(*) FROM brief_cache").fetchone()[0] == 0


@pytest.mark.parametrize("change", [{"selected_events":[]}, {"narrative_segments":None}, {"narration":"Invented content"}, {"narrative_evidence_digest":None}])
def test_invalid_briefs_not_cacheable(settings, base, change):
    brief = base.model_copy(update=change)
    assert not usable_cached_brief(brief)
    assert not Storage(settings.database_url).save_cached_brief(brief_cache_key("general",24,"mock"), brief)


def test_latest_usable_entry_and_corrupt_payload(settings, base):
    store = Storage(settings.database_url)
    key = brief_cache_key("general",24,"mock")
    store.save(base)
    assert store.save_cached_brief(key, base)
    newer = base.model_copy(update={"id":"newer", "generated_at":NOW + timedelta(seconds=1)})
    store.save(newer)
    assert store.save_cached_brief(key, newer)
    assert store.get_cached_brief(key, NOW + timedelta(seconds=2), 1800).id == "newer"
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE briefs SET payload=? WHERE id=?", ('{}', 'newer'))
    assert store.get_cached_brief(key, NOW + timedelta(seconds=2), 1800).id == base.id


def test_restart_retains_cache_and_future_entry_is_not_hit(monkeypatch, settings, base):
    client, calls, _ = harness(monkeypatch, settings, base)
    with client:
        first = client.post("/api/briefs/generate", json={"topic":"general"}).json()
    with TestClient(api.create_app(settings)) as restarted:
        assert restarted.post("/api/briefs/generate", json={"topic":"general"}).json()["is_cached"]
    assert len(calls) == 1
    assert Storage(settings.database_url).get_cached_brief(brief_cache_key("general",24,"mock"), NOW - timedelta(seconds=1), 1800) is None
    assert Storage(settings.database_url).get(first["id"]) is not None


def test_invalid_p1_reference_not_cacheable(base):
    base.selected_events[0].verified_summary.supporting_claim_ids = ["unknown"]
    assert not usable_cached_brief(base)


def test_runtime_modes_do_not_reuse_each_others_cache(monkeypatch, settings, base):
    client, calls, _ = harness(monkeypatch, settings, base)
    with client:
        mock_id = client.post("/api/briefs/generate", json={"topic":"general"}).json()["id"]
    real = settings.model_copy(update={"daybrief_mode":"real", "tavily_api_key":"test-only",
        "nebius_api_key":"test-only", "nebius_base_url":"https://example.com", "nebius_model":"test-only"})
    with TestClient(api.create_app(real)) as client:
        result = client.post("/api/briefs/generate", json={"topic":"general"}).json()
    assert result["mode"] == "real" and not result["is_cached"]
    assert result["id"] != mock_id
    assert len(calls) == 2
