import asyncio
import json
from datetime import UTC, datetime

import pytest

from daybrief.adapters import (
    STAGE_INSTRUCTIONS,
    MockModel,
    MockSearch,
    ModelOutputError,
    Nemotron,
    ProviderError,
)
from daybrief.cache import FileCache
from daybrief.config import Settings
from daybrief.models import EvidenceChecks
from daybrief.pipeline import generate_brief, run_pipeline

SEGMENTS = [{
    "claim_index": 0, "evidence_index": 0, "text": "A reported occurrence and detail.",
    "claims": [
        {"id": "c0", "text": "A reported occurrence.", "is_core": True},
        {"id": "c1", "text": "A complementary detail.", "is_core": False},
    ],
}]


def check(index=0, evidence=0, support="full"):
    return {"claim_index": index, "evidence_index": evidence, "support": support}


BAD_CHECKS = [
    pytest.param([check(), check(1)], id="extra"),
    pytest.param([check(), check()], id="duplicate"),
    pytest.param([], id="missing"),
    pytest.param([check(1)], id="out-of-range"),
    pytest.param([check(-1)], id="negative-index"),
    pytest.param([check(evidence=1)], id="unexpected-evidence"),
]


def model(tmp_path, cache=False):
    return Nemotron(Settings(
        _env_file=None, provider_cache_enabled=cache, provider_cache_dir=str(tmp_path),
        nebius_base_url="https://example.invalid", nebius_model="test",
    ))


@pytest.mark.parametrize("bad_checks", BAD_CHECKS)
def test_invalid_mapping_retries_with_specific_feedback(monkeypatch, tmp_path, bad_checks, caplog):
    calls = []

    async def respond(url, headers, payload, timeout):
        calls.append(payload)
        checks = bad_checks if len(calls) == 1 else [check()]
        return {"choices": [{"message": {"content": json.dumps({"checks": checks})}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", respond)
    result = asyncio.run(model(tmp_path).structured("narrative_verify", SEGMENTS, EvidenceChecks))
    assert result.model_dump() == {"checks": [check()]}
    assert len(calls) == 2
    feedback = calls[1]["messages"][-1]["content"]
    assert "narrative_verifier_mapping_failed" in feedback
    assert "narrative segment index" in feedback
    assert "expected_pairs" in feedback and "received_pairs" in feedback
    assert "narrative_verifier_mapping_failed" in caplog.text
    # Retry preserves the exact segment text and selected evidence.
    for payload in calls:
        assert json.loads(payload["messages"][1]["content"])["input"] == SEGMENTS


def test_repeated_mapping_failure_stops_after_two_attempts(monkeypatch, tmp_path):
    calls = []

    async def respond(*args):
        calls.append(1)
        return {"choices": [{"message": {"content": json.dumps({"checks": [check(), check(1)]})}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", respond)
    with pytest.raises(ModelOutputError, match="narrative_verifier_mapping_failed") as caught:
        asyncio.run(model(tmp_path).structured("narrative_verify", SEGMENTS, EvidenceChecks))
    assert len(calls) == 2
    assert "Narrative failed grounded evidence verification" not in str(caught.value)


def test_invalid_cached_mapping_is_revalidated_and_replaced(monkeypatch, tmp_path):
    client = model(tmp_path, cache=True)
    key = {
        "version": 1, "endpoint": client.s.nebius_base_url, "model": client.s.nebius_model,
        "stage": "narrative_verify", "input": SEGMENTS,
        "schema": EvidenceChecks.model_json_schema(), "instructions": STAGE_INSTRUCTIONS,
        "temperature": 0,
    }
    cache = FileCache(client.s)
    cache.put("model", key, {"checks": [check(), check(1)]})
    calls = []

    async def respond(url, headers, payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": json.dumps({"checks": [check()]})}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", respond)
    for _ in range(2):
        result = asyncio.run(client.structured("narrative_verify", SEGMENTS, EvidenceChecks))
        assert len(result.checks) == 1
    assert len(calls) == 1
    assert "narrative_verifier_mapping_failed" in calls[0]["messages"][-1]["content"]
    assert cache.get("model", key) == {"checks": [check()]}


@pytest.mark.parametrize("support", ["full", "partial", "none", "contradicted"])
def test_single_segment_two_claims_verifies_once_and_preserves_grounding(monkeypatch, tmp_path, support, caplog):
    now = datetime(2026, 10, 14, 12, tzinfo=UTC)
    brief = asyncio.run(run_pipeline("general", 24, now, MockSearch(), MockModel(), "mock"))
    verifier = model(tmp_path)
    calls = []

    async def respond(url, headers, payload, timeout):
        calls.append(payload)
        rows = json.loads(payload["messages"][1]["content"])["input"]
        assert len(rows) == 1 and len(rows[0]["claims"]) == 2
        assert rows[0]["claim_index"] == 0
        assert [c["id"] for c in rows[0]["claims"]] == ["c0", "c2"]
        return {"choices": [{"message": {"content": json.dumps({"checks": [check(support=support)]})}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", respond)

    class WithRealVerifierAdapter(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "narrative_verify":
                return await verifier.structured(stage, data, schema)
            return await super().structured(stage, data, schema)

    request = generate_brief(brief.selected_events[:1], brief.sources, "general", 24, now, WithRealVerifierAdapter(), "mock")
    if support == "full":
        result = asyncio.run(request)
        assert len(result.narrative_segments) == 1
    else:
        with pytest.raises(ProviderError, match="narrative_grounding_failed"):
            asyncio.run(request)
        assert "narrative_grounding_failed" in caplog.text
        assert "narrative_verifier_mapping_failed" not in caplog.text
    assert len(calls) == 1  # Non-full judgments are not retried to seek acceptance.
