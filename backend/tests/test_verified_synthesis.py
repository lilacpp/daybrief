import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from daybrief.adapters import MockModel, MockSearch, ProviderError
from daybrief.fixtures import articles
from daybrief.models import NewsEvent, VerifiedEventSummary
from daybrief.pipeline import (
    extract_event_claims,
    run_pipeline,
    synthesize_verified_event,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture
def event():
    return asyncio.run(extract_event_claims(articles("general", NOW)[:2], MockModel()))


def test_only_eligible_claims_reach_synthesis(event):
    core = event.claims[0]
    event.claims = [core, core.model_copy(update={"is_core": False})]
    for status, classification in [
        ("partially_supported", "confirmed_fact"),
        ("unsupported", "confirmed_fact"),
        ("supported", "reported_claim"),
        ("supported", "analysis"),
        ("supported", "uncertain"),
    ]:
        event.claims.append(
            core.model_copy(
                update={
                    "text": "EXCLUDED",
                    "support_status": status,
                    "classification": classification,
                }
            )
        )
    original = event.model_dump()

    class Recording(MockModel):
        async def structured(self, stage, data, schema):
            assert stage == "verified_event_synthesis"
            assert schema is VerifiedEventSummary
            assert data == {
                "claims": [
                    {"id": "c0", "text": core.text, "is_core": True},
                    {"id": "c1", "text": core.text, "is_core": False},
                ]
            }
            return await super().structured(stage, data, schema)

    result = asyncio.run(synthesize_verified_event(event, Recording()))
    assert result.verified_summary.supporting_claim_ids == ["c0", "c1"]
    assert result.verified_summary.why_it_matters is None
    assert result.model_dump(exclude={"verified_summary"}) == {
        key: value for key, value in original.items() if key != "verified_summary"
    }


def test_synthesis_requires_core_before_call(event):
    for claim in event.claims:
        claim.is_core = False

    class NeverCalled:
        async def structured(self, *args):
            pytest.fail("Background-only evidence must not trigger synthesis")

    with pytest.raises(ProviderError, match="requires a supported confirmed core"):
        asyncio.run(synthesize_verified_event(event, NeverCalled()))


@pytest.mark.parametrize("references", [["unknown"], ["c1"], ["c0", "c0"], ["c2"]])
def test_invalid_references_rejected(event, references):
    event.claims[2].is_core = False

    class Invalid:
        async def structured(self, stage, data, schema):
            return VerifiedEventSummary(
                what_happened="Summary", supporting_claim_ids=references
            )

    with pytest.raises(ProviderError, match="references"):
        asyncio.run(synthesize_verified_event(event, Invalid()))
    assert event.verified_summary is None


@pytest.mark.parametrize(
    "field,value",
    [("support_status", "partially_supported"), ("classification", "analysis")],
)
def test_reference_eligibility_rechecked_after_model(event, field, value):
    class Changed:
        async def structured(self, stage, data, schema):
            setattr(event.claims[0], field, value)
            return VerifiedEventSummary(
                what_happened="Summary", supporting_claim_ids=["c0"]
            )

    with pytest.raises(ProviderError, match="Unsafe verified synthesis"):
        asyncio.run(synthesize_verified_event(event, Changed()))


def test_summary_schema_is_strict_and_old_events_remain_readable(event):
    payload = event.model_dump(exclude={"verified_summary"})
    assert NewsEvent.model_validate(payload).verified_summary is None
    for extra in [{"extra": "no"}, {"supporting_claim_ids": []}, {"what_happened": 42}]:
        with pytest.raises(ValidationError):
            VerifiedEventSummary.model_validate(
                {
                    "what_happened": "Event",
                    "supporting_claim_ids": ["c0"],
                    **extra,
                }
            )


def test_pipeline_synthesizes_before_ranking_and_passes_summary_to_script():
    calls = []

    class Recording(MockModel):
        async def structured(self, stage, data, schema):
            calls.append(stage)
            if stage == "rank":
                assert all(event["verified_summary"] is not None for event in data)
            if stage == "script":
                assert all(event["verified_summary"] for event in data["events"])
            return await super().structured(stage, data, schema)

    brief = asyncio.run(
        run_pipeline("general", 24, NOW, MockSearch(), Recording(), "mock")
    )
    assert (
        calls.index("verify")
        < calls.index("verified_event_synthesis")
        < calls.index("rank")
        < calls.index("script")
    )
    assert len(brief.selected_events) == 3
    for paragraph, event in zip(
        brief.narration.split("\n\n")[1:-1], brief.selected_events, strict=True
    ):
        assert event.verified_summary is not None
        assert paragraph == " ".join(event.claims[index].text for index in [0, 2])


def test_pipeline_skips_invalid_synthesis():
    class Invalid(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "verified_event_synthesis":
                return VerifiedEventSummary(
                    what_happened="Bad", supporting_claim_ids=["unknown"]
                )
            assert stage not in ("rank", "script")
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="Not enough corroborated events"):
        asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), Invalid(), "mock"))
