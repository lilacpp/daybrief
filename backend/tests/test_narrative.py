import asyncio
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from daybrief.adapters import MockModel, MockSearch, Nemotron, ProviderError
from daybrief.config import Settings
from daybrief.fixtures import PARAPHRASES
from daybrief.models import EvidenceChecks, NarrativePlan, NarrativeSegment
from daybrief.pipeline import generate_brief, run_pipeline
from daybrief.storage import Storage
from daybrief.video import split_brief

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture
def original():
    return asyncio.run(
        run_pipeline("general", 24, NOW, MockSearch(), MockModel(), "mock")
    )


class ParaphrasingModel(MockModel):
    async def structured(self, stage, data, schema):
        if stage == "script":
            return NarrativePlan(
                segments=[
                    NarrativeSegment(
                        event_id=e["id"],
                        text=" ".join(
                            PARAPHRASES.get(c["text"], c["text"])
                            for c in e["claims"][:2]
                        ),
                        supporting_claim_ids=[c["id"] for c in e["claims"][:2]],
                    )
                    for e in data["events"]
                ]
            )
        if stage == "narrative_verify":
            # Deterministic entailment oracle for these known paraphrase fixtures.
            for row in data:
                assert row["text"] == " ".join(
                    PARAPHRASES.get(c["text"], c["text"]) for c in row["claims"]
                )
        return await super().structured(stage, data, schema)


def generate(original, model):
    return asyncio.run(
        generate_brief(
            original.selected_events,
            original.sources,
            "general",
            24,
            NOW,
            model,
            "mock",
        )
    )


def test_paraphrased_narrative_persists_and_drives_video(original, tmp_path):
    result = generate(original, ParaphrasingModel())
    assert result.narration != original.narration
    assert [s.event_id for s in result.narrative_segments] == [
        e.id for e in result.selected_events
    ]
    assert len(result.narrative_segments) == 3
    assert 45 <= result.estimated_duration_seconds <= 75
    assert split_brief(result)[1:-1] == [s.text for s in result.narrative_segments]
    storage = Storage(f"sqlite:///{tmp_path}/brief.db")
    storage.save(result)
    assert split_brief(storage.get(result.id)) == split_brief(result)


def test_only_verified_evidence_reaches_narrative(original):
    class Recording(ParaphrasingModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                assert data["word_budget"]["target_total_words"] == 138
                for payload, event in zip(
                    data["events"], original.selected_events, strict=True
                ):
                    assert set(payload) == {"id", "verified_summary", "claims"}
                    assert payload[
                        "verified_summary"
                    ] == event.verified_summary.model_dump(mode="json")
                    assert payload["claims"] == [
                        {"id": f"c{i}", "text": c.text, "is_core": c.is_core}
                        for i, c in enumerate(event.claims)
                        if c.support_status == "supported"
                        and c.classification == "confirmed_fact"
                    ]
            return await super().structured(stage, data, schema)

    generate(original, Recording())


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "swapped", "duplicate_event"])
def test_invalid_mapping_rejected_before_evidence_call(original, kind):
    class Bad(MockModel):
        async def structured(self, stage, data, schema):
            assert stage != "narrative_verify"
            plan = await super().structured(stage, data, schema)
            if kind == "unknown":
                plan.segments[0].supporting_claim_ids = ["c999"]
            elif kind == "duplicate":
                plan.segments[0].supporting_claim_ids = ["c0", "c0"]
            elif kind == "swapped":
                plan.segments.reverse()
            else:
                plan.segments[1].event_id = plan.segments[0].event_id
            return plan

    with pytest.raises(ProviderError):
        generate(original, Bad())


@pytest.mark.parametrize("text", ["", "   ", "Two\nparagraphs", "Bad\ttext"])
def test_empty_or_malformed_segment_rejected(text):
    with pytest.raises(ValidationError):
        NarrativeSegment(event_id="e", text=text, supporting_claim_ids=["c0"])


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_requires_exactly_three_segments(count):
    with pytest.raises(ValidationError):
        NarrativePlan(
            segments=[
                NarrativeSegment(
                    event_id=str(i), text="Fact.", supporting_claim_ids=["c0"]
                )
                for i in range(count)
            ]
        )


@pytest.mark.parametrize(
    "decision", ["partial", "none", "contradicted", "missing", "duplicate"]
)
def test_evidence_gate_rejects_unverified_prose(original, decision):
    class Bad(ParaphrasingModel):
        async def structured(self, stage, data, schema):
            if stage == "narrative_verify":
                checks = [
                    {"claim_index": i, "evidence_index": 0, "support": "full"}
                    for i in range(3)
                ]
                if decision == "missing":
                    checks.pop()
                elif decision == "duplicate":
                    checks.append(checks[0])
                else:
                    checks[0]["support"] = decision
                return EvidenceChecks.model_validate({"checks": checks})
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="grounded evidence"):
        generate(original, Bad())


def test_invented_fact_rejected_even_with_valid_ids(original):
    class Invented(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                plan = await super().structured(stage, data, schema)
                plan.segments[0].text += " Officials reported 900 deaths."
                return plan
            if stage == "narrative_verify":
                assert "900 deaths" in data[0]["text"]
                assert all(
                    "900 deaths" not in claim["text"] for claim in data[0]["claims"]
                )
                return EvidenceChecks.model_validate(
                    {
                        "checks": [
                            {
                                "claim_index": i,
                                "evidence_index": 0,
                                "support": "none" if i == 0 else "full",
                            }
                            for i in range(3)
                        ]
                    }
                )
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="grounded evidence"):
        generate(original, Invented())


@pytest.mark.parametrize("kind", ["segment", "claim", "status", "core", "digest"])
def test_video_rejects_changed_narrative_or_evidence(original, kind):
    result = generate(original, ParaphrasingModel())
    if kind == "segment":
        result.narrative_segments[0].text += " An invented fact."
        result.narration = "\n\n".join(
            [
                result.opening,
                *(s.text for s in result.narrative_segments),
                result.closing,
            ]
        )
    elif kind == "claim":
        result.selected_events[0].claims[0].text = "Changed evidence."
    elif kind == "status":
        result.selected_events[0].claims[0].support_status = "unsupported"
    elif kind == "core":
        for c in result.selected_events[0].claims:
            c.is_core = False
    else:
        result.narrative_evidence_digest = None
    with pytest.raises(ValueError):
        split_brief(result)


def test_legacy_video_still_requires_exact_claim_text(original):
    original.narrative_segments = None
    original.narrative_evidence_digest = None
    assert "\n\n".join(split_brief(original)) == original.narration
    original.narration = original.narration.replace(
        original.selected_events[0].claims[0].text, "Unverified paraphrase."
    )
    with pytest.raises(ValueError):
        split_brief(original)


def test_structured_narrative_retries_malformed_output(original, monkeypatch, tmp_path):
    calls = []

    async def respond(url, headers, payload, timeout):
        calls.append(payload)
        content = (
            {"segments": []}
            if len(calls) == 1
            else {"segments": [s.model_dump() for s in original.narrative_segments]}
        )
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", respond)
    model = Nemotron(
        Settings(
            _env_file=None,
            provider_cache_dir=str(tmp_path),
            nebius_base_url="https://example.invalid",
            nebius_model="test",
        )
    )
    plan = asyncio.run(model.structured("script", {}, NarrativePlan))
    assert len(plan.segments) == 3
    assert len(calls) == 2
    assert "previous response failed validation" in calls[1]["messages"][-1]["content"]
