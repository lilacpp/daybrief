import asyncio
from datetime import UTC, datetime

import pytest

from daybrief.adapters import MockModel, ProviderError, Tavily
from daybrief.config import Settings
from daybrief.fixtures import articles
from daybrief.pipeline import enrich_candidate_groups
from daybrief.search_queries import GENERAL_QUERIES

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def test_general_discovery_requests_thirty_and_interleaves_lanes():
    calls = []

    class Search(Tavily):
        async def _search(self, query, topic, hours, now, max_results):
            calls.append((query, max_results))
            return [f"{query}:{i}" for i in range(max_results)]

    result = asyncio.run(Search(Settings()).search_news("general", 24, NOW))
    assert calls == [(query, 10) for query in GENERAL_QUERIES]
    assert len(result) == 30
    assert result[:3] == [f"{query}:0" for query in GENERAL_QUERIES]


def test_general_discovery_preserves_successful_lanes():
    class Search(Tavily):
        async def _search(self, query, *args):
            if query == GENERAL_QUERIES[1]:
                raise ProviderError("Unavailable")
            return [query]

    assert asyncio.run(Search(Settings()).search_news("general", 24, NOW)) == [
        GENERAL_QUERIES[0], GENERAL_QUERIES[2]
    ]


def test_general_discovery_all_lanes_failed():
    class Search(Tavily):
        async def _search(self, *args):
            raise ProviderError("Unavailable")

    with pytest.raises(ProviderError, match="All general discovery queries failed"):
        asyncio.run(Search(Settings()).search_news("general", 24, NOW))


def test_general_enrichment_prioritizes_single_source_occurrence():
    data = articles("general", NOW)
    background = [data[0].model_copy(update={"title": "Historical explainer"})]
    occurrence = [data[2]]
    already_paired = data[4:6]
    calls = []

    class Search:
        async def search_event_sources(self, group, *args):
            calls.append(group)
            return []

    result = asyncio.run(enrich_candidate_groups(
        [background, already_paired, occurrence], "general", 24, NOW,
        Search(), MockModel(), 1, 1,
    ))
    assert calls == [occurrence]
    assert result == [background, already_paired, occurrence]
