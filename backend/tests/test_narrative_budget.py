import asyncio
from datetime import UTC, datetime

import pytest

from daybrief.adapters import STAGE_INSTRUCTIONS, MockModel, MockSearch, ProviderError
from daybrief.models import EvidenceChecks
from daybrief.pipeline import generate_brief, run_pipeline

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
CLAIMS = [
    "Mistral released its newest AI model, Mistral Large 4, to the public.",
    "Microsoft, Barclays Bank and supermarket chain Kroger announced AI-related workforce reductions this year.",
]


@pytest.fixture
def events():
    brief = asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), MockModel(), "mock"))
    selected = brief.selected_events[:2]
    for event, text in zip(selected, CLAIMS, strict=True):
        event.claims = [event.claims[0].model_copy(update={"text": text})]
    return selected


def generate(events, model):
    return asyncio.run(generate_brief(events, [], "general", 24, NOW, model, "mock"))


@pytest.mark.parametrize("count", [1, 2])
def test_sparse_evidence_produces_short_grounded_brief(events, count):
    class Recording(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                budget = data["word_budget"]
                assert [e["selected_evidence_words"] for e in budget["per_event"]] == [12, 13][:count]
                assert [e["target_words"] for e in budget["per_event"]] == [14, 15][:count]
                assert budget["target_total_words"] == sum([14, 15][:count]) + 8
                assert "min_total_words" not in budget
                assert "min_segment_words" not in budget
            if stage == "narrative_verify":
                for row, text in zip(data, CLAIMS[:count], strict=True):
                    assert row["text"] == text
                    assert row["claims"] == [{"id": "c0", "text": text, "is_core": True}]
            return await super().structured(stage, data, schema)

    result = generate(events[:count], Recording())
    assert [s.text for s in result.narrative_segments] == CLAIMS[:count]
    assert all(s.supporting_claim_ids == ["c0"] for s in result.narrative_segments)
    assert result.estimated_duration_seconds == round((sum([12, 13][:count]) + 8) / 2.3, 1)
    assert result.estimated_duration_seconds < 15 * count


@pytest.mark.parametrize("words,accepted", [(49, True), (50, False)])
def test_one_event_upper_word_limit_remains_enforced(events, words, accepted):
    events[0].claims[0].text = " ".join(["fact"] * words)

    class Recording(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                assert data["word_budget"]["per_event"][0]["target_words"] == 49
                assert data["word_budget"]["max_total_words"] == 57
            return await super().structured(stage, data, schema)

    if accepted:
        assert generate(events[:1], Recording()).estimated_duration_seconds <= 25
    else:
        with pytest.raises(ProviderError, match="maximum 25 second"):
            generate(events[:1], Recording())


@pytest.mark.parametrize("padding", [
    "The company made the model available to anyone.",
    "Users can obtain the model freely.",
    "They shared the news with everyone.",
])
def test_unsupported_padding_still_fails_grounding(events, padding):
    class Padding(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                result = await super().structured(stage, data, schema)
                result.segments[0].text += " " + padding
                return result
            if stage == "narrative_verify":
                assert padding in data[0]["text"]
                assert data[0]["claims"] == [{"id": "c0", "text": CLAIMS[0], "is_core": True}]
                return EvidenceChecks.model_validate({"checks": [
                    {"claim_index": 0, "evidence_index": 0, "support": "partial"}
                ]})
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="narrative_grounding_failed"):
        generate(events[:1], Padding())


def test_writer_prompt_prefers_faithfulness_over_length():
    prompt = STAGE_INSTRUCTIONS["script"]
    assert "Prefer a short faithful sentence over padding" in prompt
    assert "Never add information merely to meet a word target" in prompt
    assert "return one concise sentence" in prompt
    assert "not minimums" in prompt
    assert "public release into free access" in prompt
    assert "announcement into universal dissemination" in prompt
    assert "planned reductions into currently executing cuts" in prompt
