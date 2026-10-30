import asyncio
from datetime import UTC, datetime

import pytest

from daybrief.adapters import MockModel, MockSearch, ProviderError
from daybrief.models import NarrativePlan
from daybrief.pipeline import generate_brief, run_pipeline
from daybrief.storage import Storage
from daybrief.video import split_brief

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_safe_story_count_narration_and_persistence(count, tmp_path):
    class Search(MockSearch):
        async def search_news(self, *args):
            return (await super().search_news(*args))[:count * 2]

    result = asyncio.run(run_pipeline("general", 24, NOW, Search(), MockModel(), "mock"))
    assert len(result.selected_events) == count
    assert len(result.narrative_segments) == count
    assert bool(result.coverage_note) == (count < 3)
    assert 15 * count <= result.estimated_duration_seconds <= 25 * count
    assert all(e.corroboration_level == "independently_corroborated" for e in result.selected_events)
    assert len(split_brief(result)) == count + 2
    store = Storage(f"sqlite:///{tmp_path}/brief.db")
    store.save(result)
    assert split_brief(store.get(result.id)) == split_brief(result)
    for event, segment in zip(result.selected_events, result.narrative_segments, strict=True):
        selected = [event.claims[int(key[1:])] for key in segment.supporting_claim_ids]
        assert all(c.support_status == "supported" and c.classification == "confirmed_fact" for c in selected)
        assert any(c.is_core for c in selected)
        assert all(len({u.host for u in c.supporting_source_urls}) >= 2 for c in selected)


def test_no_safe_stories_still_rejected():
    with pytest.raises(ProviderError, match="No safely corroborated events"):
        asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), MockModel("partial"), "mock"))


def test_short_model_output_cannot_drop_selected_events():
    original = asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), MockModel(), "mock"))

    class Dropped(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                plan = await super().structured(stage, data, schema)
                return NarrativePlan(segments=plan.segments[:1])
            assert stage != "narrative_verify"
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="mapping"):
        asyncio.run(generate_brief(original.selected_events, original.sources, "general", 24, NOW, Dropped(), "mock"))


@pytest.mark.parametrize("paired,single", [(2, 1), (1, 2)])
def test_single_source_candidates_do_not_fill_missing_slots(paired, single):
    class Search(MockSearch):
        async def search_news(self, *args):
            data = await super().search_news(*args)
            return data[:paired * 2] + [data[i * 2] for i in range(paired, paired + single)]

    result = asyncio.run(run_pipeline("general", 24, NOW, Search(), MockModel(), "mock"))
    assert len(result.selected_events) == paired
    assert result.coverage_note
    assert all(e.corroboration_level == "independently_corroborated" for e in result.selected_events)
    # A full semantic verdict from one source is still insufficient under current rules.
    assert all(c.support_status == "supported" for e, segment in zip(
        result.selected_events, result.narrative_segments, strict=True
    ) for key in segment.supporting_claim_ids for c in [e.claims[int(key[1:])]])
