"""Offline occurrence-coherence regressions; no provider calls or factual assertions."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from daybrief.adapters import (
    STAGE_INSTRUCTIONS,
    MockModel,
    MockSearch,
    Nemotron,
    ProviderError,
)
from daybrief.config import Settings
from daybrief.fixtures import articles
from daybrief.models import Clusters, EventCoherence, NarrativeClaimSelection
from daybrief.pipeline import (
    EventCoherenceError,
    cluster_articles_into_events,
    event_is_corroborated,
    extract_event_claims,
    generate_brief,
    run_pipeline,
    synthesize_verified_event,
)
from daybrief.validation import validate_cluster_membership

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
# User-reported examples are synthetic regression input, not verified news.
OCCURRENCES = [
    "Trump endorsed Netanyahu for reelection.",
    "Trump waived new Russia sanctions.",
    "Trump announced a Russian diesel deal.",
    "Iran imported military hardware while evading sanctions.",
    "Lula won reelection in Brazil.",
    "A Serbian student movement defeated Vučić’s ruling party.",
]
RELATED = [
    "The city opened a new bridge.",
    "The bridge connects two previously isolated districts.",
    "Residents welcomed the bridge at its opening.",
]


@pytest.fixture
def event():
    result = asyncio.run(extract_event_claims(articles("general", NOW)[:2], MockModel()))
    result.claims = [result.claims[0].model_copy(update={"text": text, "is_core": i == 0})
                     for i, text in enumerate(RELATED)]
    asyncio.run(synthesize_verified_event(result, MockModel()))
    return result


class RejectMixture(MockModel):
    async def structured(self, stage, data, schema):
        if stage == "event_coherence":
            texts = [c["text"] for c in data["claims"]]
            assert len(texts) >= 2
            return EventCoherence(coherent=False, reason="Distinct occurrences: endorsement, sanctions waiver, diesel deal, imports and separate elections.", unrelated_core_groups=[[c["id"]] for c in data["claims"] if c["is_core"]])
        pytest.fail(f"Rejected mixed event must not reach {stage}")


def test_exact_membership_and_support_do_not_establish_coherence(event):
    rows = [{"id": f"a{i}"} for i in range(6)]
    # Demonstrates the old boundary: all articles can be mapped correctly into
    # one group while representing six different occurrences.
    validate_cluster_membership(Clusters(groups=[[r["id"] for r in rows]]), rows)
    event.claims = [event.claims[0].model_copy(update={"text": text, "is_core": True}) for text in OCCURRENCES]
    assert event_is_corroborated(event)  # Existing independent-source checks alone pass.
    with pytest.raises(EventCoherenceError, match="event_coherence_rejected"):
        asyncio.run(synthesize_verified_event(event, RejectMixture()))
    assert event.verified_summary is None


def test_distinct_occurrences_use_original_clustering_without_second_gate():
    base = articles("general", NOW)[0]
    rows = [base.model_copy(update={"id": f"a{i}-{j}", "title": text, "snippet": text})
            for i, text in enumerate(OCCURRENCES) for j in range(2)]

    class OccurrenceClustering(MockModel):
        async def structured(self, stage, data, schema):
            assert stage == "cluster"
            return await super().structured(stage, data, schema)

    groups = asyncio.run(cluster_articles_into_events(rows, OccurrenceClustering()))
    assert len(groups) == 6
    assert all(len(g) == 2 and len({a.title for a in g}) == 1 for g in groups)


def test_related_reporting_angles_remain_together():
    rows = articles("general", NOW)[:2]
    rows[1] = rows[1].model_copy(update={"title": "Residents react to the same occurrence"})

    class Related(MockModel):
        async def structured(self, stage, data, schema):
            assert stage == "cluster"
            return Clusters(groups=[[a["id"] for a in data]])

    assert len(asyncio.run(cluster_articles_into_events(rows, Related()))) == 1


def test_p2_does_not_repeat_coherence_approval(event):
    class NoRepeat(MockModel):
        async def structured(self, stage, data, schema):
            assert stage not in ("cluster_coherence", "event_coherence")
            return await super().structured(stage, data, schema)
    result = asyncio.run(generate_brief([event], [], "general", 24, NOW, NoRepeat(), "mock"))
    assert result.narrative_segments[0].text == " ".join(RELATED)


def test_core_context_reaction_selected_without_duplicate(event):
    event.claims.insert(1, event.claims[0].model_copy(deep=True))
    calls = []

    class Recording(MockModel):
        async def structured(self, stage, data, schema):
            calls.append(stage)
            if stage == "event_coherence":
                assert all(c["text"] in RELATED for c in data["claims"])
            if stage == "script":
                assert [c["text"] for c in data["events"][0]["claims"]] == RELATED
                assert data["events"][0]["supporting_claim_ids"] == ["c0", "c2", "c3"]
            if stage == "narrative_verify":
                assert [c["text"] for c in data[0]["claims"]] == RELATED
            return await super().structured(stage, data, schema)

    result = asyncio.run(generate_brief([event], [], "general", 24, NOW, Recording(), "mock"))
    assert result.narrative_segments[0].text == " ".join(RELATED)
    assert calls == ["script_select", "script", "narrative_verify"]
    assert "SAME occurrence" in STAGE_INSTRUCTIONS["script_select"]
    assert "semantically redundant" in STAGE_INSTRUCTIONS["script_select"]


def test_one_claim_event_stays_short(event):
    event.claims = event.claims[:1]
    result = asyncio.run(generate_brief([event], [], "general", 24, NOW, MockModel(), "mock"))
    assert result.narrative_segments[0].text == RELATED[0]
    assert result.narrative_segments[0].supporting_claim_ids == ["c0"]
    assert result.estimated_duration_seconds < 10


def test_three_claim_limit_is_enforced_even_without_schema_validation(event):
    event.claims.append(event.claims[0].model_copy(deep=True))

    class Four(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script_select":
                return schema.model_construct(segments=[NarrativeClaimSelection.model_construct(
                    event_id=event.id, supporting_claim_ids=["c0", "c1", "c2", "c3"],
                )])
            assert stage not in ("script", "narrative_verify")
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="Invalid narrative claim references"):
        asyncio.run(generate_brief([event], [], "general", 24, NOW, Four(), "mock"))


def test_coherence_provider_failure_warns_but_synthesis_still_runs(event, caplog):
    class Unavailable(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "event_coherence":
                raise ProviderError("offline outage")
            return await super().structured(stage, data, schema)
    diagnostics = {}
    result = asyncio.run(synthesize_verified_event(event, Unavailable(), diagnostics=diagnostics))
    assert result.verified_summary is not None
    assert diagnostics == {"coherence_warning": 1}
    assert "coherence_warning" in caplog.text


def test_original_clustering_malformed_mapping_still_retries(monkeypatch):
    from daybrief import adapters
    calls = []
    async def response(url, headers, payload, timeout):
        calls.append(payload)
        groups = [["a0"]] if len(calls) == 1 else [["a0"], ["a1"]]
        return {"choices": [{"message": {"content": json.dumps({"groups": groups})}}]}
    monkeypatch.setattr(adapters, "post_json", response)
    settings = Settings(_env_file=None, provider_cache_enabled=False, nebius_api_key="test-only")
    result = asyncio.run(Nemotron(settings).structured("cluster", [{"id":"a0"}, {"id":"a1"}], Clusters))
    assert result.groups == [["a0"], ["a1"]]
    assert len(calls) == 2
    assert "missing_ids" in str(calls[1]["messages"])


@pytest.mark.parametrize("failure", ["conservative", "provider", "schema"])
def test_conservative_reviews_do_not_collapse_story_count(caplog, failure):
    import logging
    from collections import Counter

    from daybrief.adapters import ModelOutputError

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    calls = Counter()
    class Conservative(MockModel):
        async def structured(self, stage, data, schema):
            calls[stage] += 1
            if stage == "event_coherence":
                if failure == "provider":
                    raise ProviderError("offline outage")
                if failure == "schema":
                    raise ModelOutputError("malformed diagnostic")
                return EventCoherence(coherent=False, reason="Context relation uncertain")
            return await super().structured(stage, data, schema)
    result = asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), Conservative(), "mock"))
    assert len(result.selected_events) == 3
    assert calls["cluster_coherence"] == 0
    assert calls["event_coherence"] == calls["verified_event_synthesis"] == 3
    assert calls["script_select"] == calls["script"] == calls["narrative_verify"] == 1
    assert "coherence_warning=3" in caplog.text
    assert "event_coherence_rejected=0" in caplog.text
    assert "synthesis_provider_failed=0" in caplog.text


@pytest.mark.parametrize("groups", [[['c0'], ['c1']], [['c0'], ['unknown']], [['c0'], ['c0']], [[], ['c0']]])
def test_invalid_or_noncore_conflict_cannot_reject(event, groups, caplog):
    class InvalidMapping(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "event_coherence":
                return EventCoherence(coherent=False, reason="Conservative concern", unrelated_core_groups=groups)
            return await super().structured(stage, data, schema)
    result = asyncio.run(synthesize_verified_event(event, InvalidMapping()))
    assert result.verified_summary is not None
    assert "coherence_warning" in caplog.text


def test_warning_never_bypasses_grounding(event):
    from daybrief.models import EvidenceChecks
    class Partial(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "event_coherence":
                return EventCoherence(coherent=False, reason="Context relation uncertain")
            if stage == "narrative_verify":
                return EvidenceChecks.model_validate({"checks": [{"claim_index":0, "evidence_index":0, "support":"partial"}]})
            return await super().structured(stage, data, schema)
    asyncio.run(synthesize_verified_event(event, Partial()))
    with pytest.raises(ProviderError, match="narrative_grounding_failed"):
        asyncio.run(generate_brief([event], [], "general", 24, NOW, Partial(), "mock"))


def test_unrelated_core_conflicts_have_hard_failure_counter(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    class Conflicts(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "event_coherence":
                core_ids = [c["id"] for c in data["claims"] if c["is_core"]]
                assert len(core_ids) >= 2
                return EventCoherence(coherent=False, reason="Explicit unrelated occurrences fixture", unrelated_core_groups=[[i] for i in core_ids])
            assert stage != "verified_event_synthesis"
            return await super().structured(stage, data, schema)
    with pytest.raises(ProviderError, match="No safely corroborated events"):
        asyncio.run(run_pipeline("general", 24, NOW, MockSearch(), Conflicts(), "mock"))
    assert "event_coherence_rejected=3" in caplog.text
    assert "coherence_warning=0" in caplog.text
