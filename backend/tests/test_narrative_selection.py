import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from daybrief.adapters import MockModel, MockSearch, ProviderError
from daybrief.models import EvidenceChecks
from daybrief.pipeline import generate_brief, run_pipeline

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
NUMERICAL_DETAIL = "The job cuts of the AI era are affecting tens of thousands of people."


@pytest.fixture
def event():
    brief = asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), MockModel(), "mock"))
    result = brief.selected_events[0]
    template = result.claims[0]
    result.claims = [template.model_copy(update={"text": text, "is_core": core}) for text, core in [
        ("Microsoft, Barclays Bank and supermarket chain Kroger are among the latest entries on a growing list of companies that have announced AI-related workforce reductions this year.", True),
        ("Companies around the world are increasingly citing artificial intelligence when slashing jobs.", False),
        (NUMERICAL_DETAIL, False),
    ]]
    result.headline = NUMERICAL_DETAIL
    result.verified_summary.what_happened = NUMERICAL_DETAIL
    result.verified_summary.why_it_matters = "A numerical detail that must not leak."
    result.verified_summary.uncertainty = "Unselected uncertainty must not leak."
    return result


def generate(event, model):
    return asyncio.run(generate_brief([event], [], "general", 24, NOW, model, "mock"))


def test_select_then_write_only_selected_claims(event):
    calls = []

    class Recording(MockModel):
        async def structured(self, stage, data, schema):
            calls.append(stage)
            if stage == "script_select":
                assert set(data["events"][0]) == {"id", "claims"}
                assert [c["id"] for c in data["events"][0]["claims"]] == ["c0", "c1", "c2"]
            if stage == "script":
                payload = data["events"][0]
                assert set(payload) == {"id", "claims", "supporting_claim_ids"}
                assert payload["supporting_claim_ids"] == ["c0", "c1"]
                assert [c["text"] for c in payload["claims"]] == [c.text for c in event.claims[:2]]
                assert "tens of thousands" not in str(data)
                assert "uncertainty" not in str(data)
            if stage == "narrative_verify":
                assert [c["id"] for c in data[0]["claims"]] == ["c0", "c1"]
                assert "tens of thousands" not in str(data)
            return await super().structured(stage, data, schema)

    result = generate(event, Recording())
    assert calls == ["script_select", "script", "narrative_verify"]
    assert result.narrative_segments[0].supporting_claim_ids == ["c0", "c1"]
    assert "tens of thousands" not in result.narration
    assert result.narrative_segments[0].text == " ".join(c.text for c in event.claims[:2])


def test_uncited_numerical_detail_still_fails_semantic_gate(event):
    class Leaking(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                assert "tens of thousands" not in str(data)
                plan = await super().structured(stage, data, schema)
                # Simulate hallucinated detail despite the restricted input.
                plan.segments[0].text += " The shift affects tens of thousands of employees."
                return plan
            if stage == "narrative_verify":
                assert "tens of thousands" in data[0]["text"]
                assert all("tens of thousands" not in c["text"] for c in data[0]["claims"])
                return EvidenceChecks.model_validate({"checks": [
                    {"claim_index": 0, "evidence_index": 0, "support": "partial"}
                ]})
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="grounded evidence"):
        generate(event, Leaking())


@pytest.mark.parametrize("references", [["c0"], ["c1", "c0"], ["c0", "c2"]])
def test_writer_cannot_change_selected_claims(event, references):
    class Changed(MockModel):
        async def structured(self, stage, data, schema):
            assert stage != "narrative_verify"
            plan = await super().structured(stage, data, schema)
            if stage == "script":
                plan.segments[0].supporting_claim_ids = references
            return plan

    with pytest.raises(ProviderError, match="references changed"):
        generate(event, Changed())


@pytest.mark.parametrize("references", [[], ["c0", "c1", "c2", "c3"], ["c1"], ["c9"], ["c0", "c0"]])
def test_invalid_selection_rejected_before_writing(event, references):
    class Invalid(MockModel):
        async def structured(self, stage, data, schema):
            assert stage == "script_select"
            return schema.model_validate({"segments": [
                {"event_id": event.id, "supporting_claim_ids": references}
            ]})

    with pytest.raises((ProviderError, ValidationError)):
        generate(event, Invalid())


@pytest.mark.parametrize("status,classification", [
    ("unsupported", "confirmed_fact"), ("partially_supported", "confirmed_fact"),
    ("supported", "reported_claim"), ("supported", "analysis"), ("supported", "uncertain"),
])
def test_selector_cannot_choose_ineligible_claim(event, status, classification):
    event.claims[1].support_status = status
    event.claims[1].classification = classification

    class Invalid(MockModel):
        async def structured(self, stage, data, schema):
            assert stage == "script_select"
            assert "c1" not in [c["id"] for c in data["events"][0]["claims"]]
            return schema.model_validate({"segments": [
                {"event_id": event.id, "supporting_claim_ids": ["c0", "c1"]}
            ]})

    with pytest.raises(ProviderError, match="Unsafe narrative claim reference"):
        generate(event, Invalid())


def test_mock_selection_skips_duplicate_and_keeps_complement(event):
    event.claims.insert(1, event.claims[0].model_copy(deep=True))
    result = generate(event, MockModel())
    assert result.narrative_segments[0].supporting_claim_ids == ["c0", "c2"]
    assert "tens of thousands" not in result.narration


def test_single_selected_core_claim_succeeds(event):
    class Single(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script_select":
                return schema.model_validate({"segments": [
                    {"event_id": event.id, "supporting_claim_ids": ["c0"]}
                ]})
            return await super().structured(stage, data, schema)

    result = generate(event, Single())
    assert result.narrative_segments[0].supporting_claim_ids == ["c0"]
    assert result.narrative_segments[0].text == event.claims[0].text


def test_selection_from_wrong_event_rejected_before_writing(event):
    class WrongEvent(MockModel):
        async def structured(self, stage, data, schema):
            assert stage == "script_select"
            return schema.model_validate({"segments": [
                {"event_id": "another-event", "supporting_claim_ids": ["c0", "c1"]}
            ]})

    with pytest.raises(ProviderError, match="mapping"):
        generate(event, WrongEvent())
