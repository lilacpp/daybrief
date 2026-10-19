import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from daybrief.adapters import MockModel, MockSearch, Nemotron, ProviderError
from daybrief.api import create_app
from daybrief.config import Settings
from daybrief.fixtures import articles
from daybrief.models import Claim, EvidenceChecks, NewsEvent
from daybrief.pipeline import (
    cluster_articles_into_events,
    extract_event_claims,
    filter_recent_articles,
    normalize_articles,
    run_pipeline,
    select_diverse_events,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_provider_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_CACHE_DIR", str(tmp_path / "cache"))


def run(coro):
    return asyncio.run(coro)


def test_date_window():
    data = articles("general", NOW)
    assert len(filter_recent_articles(data, 24, NOW)) == 7
    assert not filter_recent_articles(data, 1, NOW)
    assert not filter_recent_articles(
        [data[0].model_copy(update={"published_at": NOW + timedelta(hours=1)})], 24, NOW
    )


def test_normalization():
    a = articles("general", NOW)[0]
    a = a.model_copy(
        update={
            "url": type(a.url)(
                "https://WWW.Publisher1.example/story/0/?utm_source=x#f"
            ),
            "publisher": " Publisher 1 ",
        }
    )
    normalized = normalize_articles([a, articles("general", NOW)[0]])
    assert len(normalized) == 1
    assert normalized[0].publisher == "publisher 1"
    assert str(normalized[0].url) == "https://publisher1.example/story/0"


def test_duplicate_events():
    groups = run(cluster_articles_into_events(articles("general", NOW), MockModel()))
    assert len(groups) == 5
    assert len(groups[0]) == 2


def claim_payload(**overrides):
    d = {
        "text": "Claim",
        "classification": "confirmed_fact",
        "supporting_source_urls": [],
        "evidence": [],
        "support_status": "supported",
    }
    d.update(overrides)
    return d


@pytest.mark.parametrize(
    "field,value", [("classification", "fact"), ("support_status", "yes")]
)
def test_claim_validation(field, value):
    d = claim_payload()
    d[field] = value
    with pytest.raises(ValidationError):
        Claim.model_validate(d)


def brief():
    return run(run_pipeline("general", 24, NOW, MockSearch(), MockModel(), "mock"))


def test_unsupported_excluded_and_sources():
    b = brief()
    assert "unsupported prediction" not in b.narration
    for e in b.selected_events:
        for c in e.claims:
            assert set(c.supporting_source_urls) <= set(e.source_urls)
    assert len({e.id for e in b.selected_events}) == 3
    assert 45 <= b.estimated_duration_seconds <= 75
    assert len(b.sources) == 6


def test_diversity():
    b = brief()
    assert len({e.category for e in b.selected_events}) == 3
    same = [
        e.model_copy(update={"id": str(i), "category": "same"})
        for i, e in enumerate(b.selected_events)
    ]
    with pytest.raises(ProviderError):
        select_diverse_events(same)


def test_sensitive_tone():
    b = brief()
    e = next(e for e in b.selected_events if e.category == "public_safety")
    assert e.sensitivity == "sensitive"
    assert "joke" not in b.narration.lower()
    assert "Flood" not in b.narration  # no unchecked headline added
    assert "evacuation order" in b.narration
    assert "why it matters" not in b.narration.lower()


def test_single_source_downgrade():
    e = run(extract_event_claims([articles("general", NOW)[-2]], MockModel()))
    assert e.claims[0].classification == "reported_claim"
    assert e.claims[0].support_status == "partially_supported"
    assert any("Insufficiently corroborated" in u for u in e.uncertainties)


def test_invalid_event_schema():
    d = brief().selected_events[0].model_dump()
    d["importance_score"] = 200
    with pytest.raises(ValidationError):
        NewsEvent.model_validate(d)


@pytest.fixture
def client(tmp_path):
    return TestClient(
        create_app(
            Settings(daybrief_mode="mock", database_url=f"sqlite:///{tmp_path}/test.db")
        )
    )


def test_api_success_persistence(client):
    assert client.get("/health").json()["mode"] == "mock"
    assert len(client.get("/api/topics").json()) == 3
    r = client.post(
        "/api/briefs/generate", json={"topic": "ai_technology", "lookback_hours": 24}
    )
    assert r.status_code == 200
    assert client.get("/api/briefs/" + r.json()["id"]).json() == r.json()


def test_api_errors(client):
    assert (
        client.post("/api/briefs/generate", json={"topic": "invalid"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/briefs/generate", json={"topic": "general", "lookback_hours": 0}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/briefs/generate", json={"topic": "general", "lookback_hours": 1}
        ).status_code
        == 502
    )
    assert client.get("/api/briefs/missing").status_code == 404


def test_real_mode_never_falls_back(tmp_path):
    c = TestClient(
        create_app(
            Settings(
                daybrief_mode="real",
                tavily_api_key="",
                nebius_api_key="",
                nebius_model="",
                nebius_base_url="",
                database_url=f"sqlite:///{tmp_path}/test.db",
            )
        )
    )
    assert c.post("/api/briefs/generate", json={"topic": "general"}).status_code == 503


def test_invalid_model_output_retries(monkeypatch):
    calls = []

    async def fake(*args):
        calls.append(1)
        return {"choices": [{"message": {"content": '{"groups": "invalid"}'}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    with pytest.raises(ProviderError):
        run(
            Nemotron(Settings()).structured(
                "cluster",
                [],
                __import__("daybrief.models", fromlist=["Clusters"]).Clusters,
            )
        )
    assert len(calls) == 2


def test_bad_cluster_membership():
    class Bad:
        async def structured(self, *args):
            from daybrief.models import Clusters

            return Clusters(groups=[["unknown"]])

    with pytest.raises(ProviderError):
        run(cluster_articles_into_events(articles("general", NOW), Bad()))


def test_bad_script_rejected():
    from daybrief.models import ScriptPlan

    class Bad(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "script":
                return ScriptPlan(claim_indices={e["id"]: [1] for e in data})
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError):
        run(run_pipeline("general", 24, NOW, MockSearch(), Bad(), "mock"))


def test_real_adapters_without_network(monkeypatch):
    from daybrief.adapters import Tavily

    calls = []

    async def fake(url, headers, payload, timeout):
        calls.append((url, headers, payload))
        return {
            "results": [
                {
                    "title": "Test",
                    "url": "https://news.example/test",
                    "content": "Test content",
                    "published_date": NOW.isoformat(),
                }
            ]
        }

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    data = run(Tavily(Settings(tavily_api_key="test")).search_news("science", 24, NOW))
    assert len(data) == 1
    assert calls[0][2]["topic"] == "news"
    assert calls[0][1]["Authorization"] == "Bearer test"


@pytest.mark.parametrize("topic", ["general", "ai_technology", "science"])
def test_each_topic_full_pipeline(topic):
    b = run(run_pipeline(topic, 24, NOW, MockSearch(), MockModel(), "mock"))
    assert len(b.selected_events) == 3
    assert len({e.category for e in b.selected_events}) == 3
    assert any(e.sensitivity == "sensitive" for e in b.selected_events)
    assert 45 <= b.estimated_duration_seconds <= 75


def test_sensitive_humor_guard():
    from daybrief.pipeline import enforce_editorial_tone

    e = next(e for e in brief().selected_events if e.sensitivity == "sensitive")
    with pytest.raises(ProviderError):
        enforce_editorial_tone(e, "A hilarious joke!")
    enforce_editorial_tone(e, "Authorities issued an evacuation order.")


def test_tavily_rfc_publication_date(monkeypatch):
    from daybrief.adapters import Tavily

    async def fake(*args):
        return {
            "results": [
                {
                    "title": "Test",
                    "url": "https://news.example/test",
                    "content": "Test",
                    "published_date": "Wed, 14 Oct 2026 12:00:00 GMT",
                }
            ]
        }

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    data = run(Tavily(Settings()).search_news("general", 24, NOW))
    assert data[0].published_at == NOW


def test_strict_claim_rejects_coercion():
    with pytest.raises(ValidationError):
        Claim.model_validate(claim_payload(text=123))


def pair_articles():
    return articles("general", NOW)[:2]


def extract_with(claims, verify_support="full", sources=None):
    sources = sources or pair_articles()

    class Scripted(MockModel):
        def __init__(self):
            super().__init__(verify_support=verify_support)

        async def structured(self, stage, data, schema):
            if stage != "extract":
                return await super().structured(stage, data, schema)
            first = data[0]
            payload = {
                "id": first["id"],
                "headline": first["title"],
                "category": "technology",
                "what_happened": claims[0]["text"],
                "why_it_matters": "This development may affect public decisions.",
                "uncertainties": ["Long-term outcomes remain unclear."],
                "claims": claims,
                "source_urls": [a["url"] for a in data],
                "earliest_published_at": min(a["published_at"] for a in data),
                "latest_published_at": max(a["published_at"] for a in data),
                "importance_score": 90,
                "sensitivity": "normal",
                "selection_reason": "",
            }
            payload = {key: payload[key] for key in schema.model_fields}
            payload["claims"] = [
                {key: claim[key] for key in ("text", "classification", "evidence")}
                for claim in claims
            ]
            ids = {a["url"]: a["id"] for a in data}
            for claim in payload["claims"]:
                claim["evidence"] = [
                    {
                        "source_id": ids.get(e["source_url"], "unknown"),
                        "quote": e["quote"],
                    }
                    for e in claim["evidence"]
                ]
            return schema.model_validate_json(json.dumps(payload))

    return run(extract_event_claims(sources, Scripted()))


def test_paraphrased_quotes_can_support_one_fact():
    sources = pair_articles()
    claim_text = "Researchers released an open compute benchmark for evaluating inference efficiency."
    event = extract_with(
        [
            claim_payload(
                text=claim_text,
                supporting_source_urls=[str(a.url) for a in sources],
                evidence=[
                    {"source_url": str(a.url), "quote": a.snippet} for a in sources
                ],
            )
        ],
        sources=sources,
    )
    assert event.claims[0].classification == "confirmed_fact"
    assert event.claims[0].support_status == "supported"
    assert len(event.claims[0].evidence) == 2
    assert sources[0].snippet != sources[1].snippet
    assert claim_text not in sources[1].snippet


def test_fabricated_quote_is_rejected():
    sources = pair_articles()
    event = extract_with(
        [
            claim_payload(
                text="Researchers released an open compute benchmark.",
                supporting_source_urls=[str(a.url) for a in sources],
                evidence=[
                    {
                        "source_url": str(a.url),
                        "quote": "Officials confirmed a secret moon base.",
                    }
                    for a in sources
                ],
                support_status="supported",
            )
        ],
        sources=sources,
    )
    assert event.claims[0].evidence == []
    assert event.claims[0].support_status == "unsupported"
    assert event.claims[0].classification == "reported_claim"


def test_empty_evidence_is_rejected_even_if_model_marks_supported():
    event = extract_with(
        [
            claim_payload(
                text="Researchers released an open compute benchmark.",
                supporting_source_urls=[str(a.url) for a in pair_articles()],
                evidence=[],
                support_status="supported",
            )
        ]
    )
    assert event.claims[0].support_status == "unsupported"


def test_irrelevant_evidence_is_rejected_by_model_check():
    sources = pair_articles()
    event = extract_with(
        [
            claim_payload(
                text="Researchers released an open compute benchmark for evaluating inference efficiency.",
                supporting_source_urls=[str(a.url) for a in sources],
                evidence=[
                    {"source_url": str(a.url), "quote": a.snippet} for a in sources
                ],
            )
        ],
        verify_support="none",
        sources=sources,
    )
    assert event.claims[0].evidence == []
    assert event.claims[0].support_status == "unsupported"


def test_numeric_contradiction_is_rejected():
    sources = pair_articles()
    event = extract_with(
        [
            claim_payload(
                text="Researchers released 30 open compute benchmarks.",
                supporting_source_urls=[str(a.url) for a in sources],
                evidence=[
                    {"source_url": str(a.url), "quote": a.snippet} for a in sources
                ],
            )
        ],
        verify_support="contradicted",
        sources=sources,
    )
    assert event.claims[0].support_status == "unsupported"
    assert event.claims[0].classification == "reported_claim"


def test_single_independent_source_is_not_enough():
    event = run(extract_event_claims([articles("general", NOW)[-2]], MockModel()))
    assert event.claims[0].classification == "reported_claim"
    assert event.claims[0].support_status == "partially_supported"
    assert len({item.source_url for item in event.claims[0].evidence}) == 1


def test_quote_normalization_allows_case_and_spacing():
    sources = pair_articles()
    quote = "  RESEARCHERS released   an open compute benchmark for evaluating inference efficiency. "
    event = extract_with(
        [
            claim_payload(
                text="Researchers released an open compute benchmark for evaluating inference efficiency.",
                supporting_source_urls=[str(sources[0].url), str(sources[1].url)],
                evidence=[
                    {"source_url": str(sources[0].url), "quote": quote},
                    {"source_url": str(sources[1].url), "quote": sources[1].snippet},
                ],
            )
        ],
        sources=sources,
    )
    assert event.claims[0].support_status == "supported"


def test_foreign_evidence_url_is_rejected():
    sources = pair_articles()
    event = extract_with(
        [
            claim_payload(
                text="Researchers released an open compute benchmark for evaluating inference efficiency.",
                evidence=[
                    {
                        "source_url": "https://other.example/story",
                        "quote": sources[0].snippet,
                    },
                    {"source_url": str(sources[0].url), "quote": sources[0].snippet},
                ],
            )
        ],
        sources=sources,
    )
    assert event.claims[0].support_status == "partially_supported"
    assert [str(item.source_url) for item in event.claims[0].evidence] == [
        str(sources[0].url)
    ]


def test_incomplete_verification_does_not_pass():
    class Incomplete(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "verify":
                return EvidenceChecks(checks=[])
            return await super().structured(stage, data, schema)

    with pytest.raises(ProviderError, match="Incomplete evidence verification"):
        run(extract_event_claims(pair_articles(), Incomplete()))


def test_insufficient_events_include_stage_counts():
    class EmptySearch:
        async def search_news(self, topic, hours, now):
            return []

    with pytest.raises(ProviderError, match="retrieved=0, filtered=0"):
        run(run_pipeline("general", 24, NOW, EmptySearch(), MockModel(), "mock"))


def test_enrichment_recovers_three_single_source_events():
    data = articles("general", NOW)[:6]

    class Search:
        calls = 0

        async def search_news(self, *args):
            return data[::2]

        async def search_event_sources(self, group, topic, hours, now, attempt):
            self.calls += 1
            return [a for a in data if a.title == group[0].title]

    search = Search()
    result = run(run_pipeline("general", 24, NOW, search, MockModel(), "mock"))
    assert len(result.selected_events) == 3
    assert len(result.sources) == 6
    assert len({a.id for a in result.sources}) == 6
    assert search.calls == 6


def test_enrichment_filters_unrelated_old_future_and_duplicate_results():
    from daybrief.pipeline import enrich_candidate_groups

    data = articles("general", NOW)
    group = normalize_articles([data[0]])

    class Search:
        calls = 0

        async def search_event_sources(self, *args):
            self.calls += 1
            return [
                data[0],
                data[1],
                data[2],
                data[-1],
                data[1].model_copy(
                    update={
                        "url": type(data[1].url)("https://future.example/story"),
                        "published_at": NOW + timedelta(hours=1),
                    }
                ),
            ]

    search = Search()
    groups = run(
        enrich_candidate_groups(
            [group],
            "general",
            24,
            NOW,
            search,
            MockModel(),
            1,
            2,
        )
    )
    assert {str(a.url) for a in groups[0]} == {str(a.url) for a in data[:2]}
    assert search.calls == 2


def test_enrichment_budget_and_failure_keep_original_sources():
    from daybrief.pipeline import enrich_candidate_groups

    groups = [[a] for a in normalize_articles(articles("general", NOW)[::2])]

    class Search:
        calls = 0

        async def search_event_sources(self, *args):
            self.calls += 1
            raise ProviderError("unavailable")

    search = Search()
    result = run(
        enrich_candidate_groups(
            groups,
            "general",
            24,
            NOW,
            search,
            MockModel(),
            2,
            2,
        )
    )
    assert result == groups
    assert search.calls == 4
    run(enrich_candidate_groups(groups, "general", 24, NOW, search, MockModel(), 0, 2))
    assert search.calls == 4


def test_enrichment_rejects_split_seed():
    from daybrief.models import Clusters
    from daybrief.pipeline import enrich_candidate_groups

    data = normalize_articles(articles("general", NOW)[:3])

    class Search:
        async def search_event_sources(self, *args):
            return [data[2]]

    class Split:
        async def structured(self, stage, payload, schema):
            return Clusters(groups=[[a["id"]] for a in payload])

    result = run(
        enrich_candidate_groups(
            [data[:2]],
            "general",
            24,
            NOW,
            Search(),
            Split(),
            1,
            1,
        )
    )
    assert result == [data[:2]]


def test_extraction_failure_reports_completed_events(caplog):
    class FailsSecond(MockModel):
        calls = 0

        async def structured(self, stage, data, schema):
            if stage == "extract":
                self.calls += 1
                if self.calls == 2:
                    raise ProviderError("bad extraction")
            return await super().structured(stage, data, schema)

    with (
        caplog.at_level("INFO", logger="uvicorn.error"),
        pytest.raises(
            ProviderError, match=r"stage=select; .*events=3.*extraction_failed=1"
        ),
    ):
        run(run_pipeline("general", 24, NOW, MockSearch(), FailsSecond(), "mock"))
    assert "extraction_not_started" in caplog.text
    assert "Skipping cluster=2/4: bad extraction" in caplog.text
    assert "failed:select" in caplog.text


def test_multiple_searches_have_stable_distinct_article_ids(monkeypatch):
    from daybrief.adapters import Tavily

    responses = [
        "https://one.example/story",
        "https://two.example/story",
        "https://one.example/story",
    ]
    queries = []

    async def fake(url, headers, payload, timeout):
        queries.append(payload["query"])
        return {
            "results": [
                {
                    "title": "Event",
                    "url": responses.pop(0),
                    "content": "Evidence",
                    "published_date": NOW.isoformat(),
                }
            ]
        }

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    search = Tavily(Settings())
    first = run(search.search_news("general", 24, NOW))
    second = run(search.search_event_sources(first, "general", 24, NOW, 0))
    third = run(search.search_event_sources(first, "general", 24, NOW, 1))
    assert first[0].id != second[0].id
    assert first[0].id == third[0].id
    assert queries[1] == "Event"
    assert queries[2] != queries[1]


@pytest.mark.parametrize(
    "bad,expected",
    [
        ({"choices": [{"message": {"content": '{"groups": "invalid"}'}}]}, "groups"),
        ({"choices": []}, "response_shape"),
        ({"choices": [{"message": {"content": "not json"}}]}, "json_invalid"),
    ],
)
def test_model_retry_feedback_repairs_output(monkeypatch, caplog, bad, expected):
    from daybrief.models import Clusters

    calls = []

    async def fake(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return bad
        return {"choices": [{"message": {"content": '{"groups": [["a"]]}'}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    result = run(Nemotron(Settings()).structured("cluster", [], Clusters))
    assert result.groups == [["a"]]
    assert len(calls) == 2
    assert expected in calls[1]["messages"][-1]["content"]
    assert "corrected complete JSON" in calls[1]["messages"][-1]["content"]
    assert expected in caplog.text


def test_extract_schema_excludes_computed_fields():
    from daybrief.models import ExtractedClaim, ExtractedEvent

    assert set(ExtractedEvent.model_fields) == {
        "headline",
        "category",
        "why_it_matters",
        "uncertainties",
        "claims",
        "sensitivity",
    }
    assert set(ExtractedClaim.model_fields) == {"text", "classification", "evidence"}
    group = pair_articles()
    event = run(extract_event_claims(group, MockModel()))
    assert event.source_urls == [a.url for a in group]
    assert event.earliest_published_at == min(a.published_at for a in group)
    assert event.importance_score == 0
    assert event.claims[0].support_status == "supported"
    assert event.claims[1].support_status == "unsupported"


def test_failed_candidate_does_not_block_three_valid_events(caplog):
    class Search:
        async def search_news(self, *args):
            data = articles("general", NOW)
            return [data[-2], *data[:6]]

    class FailFirst(MockModel):
        calls = 0

        async def structured(self, stage, data, schema):
            if stage == "extract":
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError("Invalid model output in extract")
            return await super().structured(stage, data, schema)

    model = FailFirst()
    result = run(run_pipeline("general", 24, NOW, Search(), model, "mock"))
    assert model.calls == 4
    assert len(result.selected_events) == 3
    assert len(result.sources) == 6
    assert "Skipping cluster=1/4" in caplog.text


def test_all_failed_candidates_stop_before_ranking():
    class FailAll(MockModel):
        calls = 0

        async def structured(self, stage, data, schema):
            assert stage not in ("rank", "script")
            if stage == "extract":
                self.calls += 1
                raise ProviderError("invalid extraction")
            return await super().structured(stage, data, schema)

    model = FailAll()
    with pytest.raises(
        ProviderError, match=r"stage=select; .*events=0.*extraction_failed=4"
    ):
        run(run_pipeline("general", 24, NOW, MockSearch(), model, "mock"))
    assert model.calls == 4


def test_extract_validation_error_reports_field_without_source_text(
    monkeypatch, caplog
):
    from daybrief.models import ExtractedEvent

    async def fake(*args):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "headline": "Test",
                                "category": "science",
                                "why_it_matters": "Context",
                                "uncertainties": [],
                                "sensitivity": "normal",
                                "claims": [
                                    {
                                        "text": "A fact",
                                        "classification": "confirmed_fact",
                                        "evidence": [
                                            {
                                                "source_url": "https://example.com/story",
                                                "quote": "PRIVATE_SOURCE_TEXT" * 40,
                                            }
                                        ],
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    with pytest.raises(ProviderError, match="claims.0.evidence.0.quote") as error:
        run(Nemotron(Settings()).structured("extract", [], ExtractedEvent))
    assert "string_too_long" in str(error.value)
    assert "PRIVATE_SOURCE_TEXT" not in str(error.value)
    assert "PRIVATE_SOURCE_TEXT" not in caplog.text


@pytest.mark.parametrize(
    "classification,urls,status,reasons",
    [
        (
            "confirmed_fact",
            [],
            "unsupported",
            "no_valid_evidence,classification_not_confirmed_fact",
        ),
        (
            "confirmed_fact",
            ["https://one.example/a", "https://one.example/b"],
            "partially_supported",
            "single_hostname,classification_not_confirmed_fact",
        ),
        (
            "reported_claim",
            ["https://one.example/a", "https://two.example/b"],
            "supported",
            "classification_not_confirmed_fact",
        ),
        (
            "confirmed_fact",
            ["https://one.example/a", "https://two.example/b"],
            "supported",
            "none",
        ),
    ],
)
def test_claim_support_diagnostics(caplog, classification, urls, status, reasons):
    from daybrief.pipeline import apply_claim_support_status

    event = run(extract_event_claims(pair_articles(), MockModel()))
    event.claims = [
        Claim.model_validate_json(
            json.dumps(
                claim_payload(
                    classification=classification,
                    evidence=[{"source_url": url, "quote": "Evidence"} for url in urls],
                )
            )
        )
    ]
    caplog.clear()
    with caplog.at_level("INFO", logger="uvicorn.error"):
        apply_claim_support_status(event, pair_articles())
    assert f"event={event.id} claim_index=0" in caplog.text
    assert f"original_classification={classification}" in caplog.text
    assert f"classification={event.claims[0].classification}" in caplog.text
    assert f"valid_evidence={len(urls)}" in caplog.text
    assert (
        f"distinct_hostnames={0 if not urls else 1 if status == 'partially_supported' else 2}"
        in caplog.text
    )
    assert f"support_status={status}" in caplog.text
    assert f"reasons={reasons}" in caplog.text
    assert f"eligible_claims={int(reasons == 'none')}" in caplog.text


def test_empty_event_support_diagnostics(caplog):
    from daybrief.pipeline import apply_claim_support_status

    event = run(extract_event_claims(pair_articles(), MockModel()))
    event.claims = []
    caplog.clear()
    with caplog.at_level("INFO", logger="uvicorn.error"):
        apply_claim_support_status(event, pair_articles())
    assert (
        "claims=0 eligible_claims=0 corroborated=False reason=no_claims" in caplog.text
    )


def test_event_details_preserve_full_sources_and_evidence_snapshots(caplog):
    sources = pair_articles()
    with caplog.at_level("INFO", logger="uvicorn.error"):
        event = run(extract_event_claims(sources, MockModel()))
    snapshots = [
        json.loads(record.getMessage().removeprefix("Pipeline details "))
        for record in caplog.records
        if record.getMessage().startswith("Pipeline details ")
    ]
    by_stage = {row["stage"]: row for row in snapshots}
    assert by_stage["event_extracted"]["sources"][0]["snippet"] == sources[0].snippet
    assert (
        by_stage["event_extracted"]["event"]["claims"][1]["classification"]
        == "confirmed_fact"
    )
    assert (
        by_stage["event_final"]["event"]["claims"][1]["classification"]
        == "reported_claim"
    )
    assert by_stage["event_final"]["corroborated"] is True
    assert by_stage["verification_input"]["pairs"][0]["quote"] == sources[0].snippet
    assert by_stage["verification_output"]["checks"][0]["support"] == "full"
    assert all(row["event_id"] == event.id for row in snapshots)


def test_search_cache_reused_across_instances(monkeypatch):
    from daybrief.adapters import Tavily

    calls = []

    async def fake(*args):
        calls.append(1)
        return {
            "results": [
                {
                    "title": "Cached article",
                    "url": "https://news.example/a",
                    "content": "Complete snippet",
                    "published_date": NOW.isoformat(),
                }
            ]
        }

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    settings = Settings(provider_cache_enabled=True)
    first = run(Tavily(settings).search_news("general", 24, NOW))
    second = run(
        Tavily(settings).search_news("general", 24, NOW + timedelta(minutes=1))
    )
    assert first == second
    assert len(calls) == 1
    from pathlib import Path

    saved = json.loads(
        next((Path(settings.provider_cache_dir) / "search").glob("*.json")).read_text()
    )
    assert saved["value"]["raw_results"][0]["content"] == "Complete snippet"


def test_model_cache_reuse_and_input_invalidation(monkeypatch):
    from daybrief.models import Clusters

    calls = []

    async def fake(*args):
        calls.append(1)
        return {"choices": [{"message": {"content": '{"groups": [["a"]]}'}}]}

    monkeypatch.setattr("daybrief.adapters.post_json", fake)
    settings = Settings(provider_cache_enabled=True)
    for _ in range(2):
        assert run(
            Nemotron(settings).structured("cluster", [{"id": "a"}], Clusters)
        ).groups == [["a"]]
    assert len(calls) == 1
    run(
        Nemotron(settings).structured(
            "cluster", [{"id": "a", "title": "New"}], Clusters
        )
    )
    assert len(calls) == 2


def test_cache_expiry_corruption_and_disable(tmp_path, monkeypatch):
    from daybrief.cache import FileCache

    settings = Settings(provider_cache_dir=str(tmp_path), provider_cache_ttl_seconds=10)
    cache = FileCache(settings)
    monkeypatch.setattr("daybrief.cache.time.time", lambda: 100)
    cache.put("search", {"query": "test"}, {"data": 1})
    assert cache.get("search", {"query": "test"}) == {"data": 1}
    monkeypatch.setattr("daybrief.cache.time.time", lambda: 111)
    assert cache.get("search", {"query": "test"}) is None
    cache.path("search", {"query": "test"}).write_text("broken json")
    assert cache.get("search", {"query": "test"}) is None
    settings.provider_cache_enabled = False
    disabled = FileCache(settings)
    disabled.put("model", {}, {"data": 2})
    assert not disabled.path("model", {}).exists()
    assert disabled.get("search", {"query": "test"}) is None


@pytest.mark.parametrize(
    "repair_kind,expected_count",
    [("valid", 2), ("wrong_quote", 1), ("unknown_id", 1), ("partial", 1)],
)
def test_repaired_evidence_must_pass_source_quote_and_semantic_checks(
    repair_kind, expected_count
):
    from daybrief.pipeline import repair_event_evidence

    group = pair_articles()
    event = run(extract_event_claims(group, MockModel()))
    event.claims = [event.claims[0]]
    event.claims[0].evidence = event.claims[0].evidence[:1]
    lookup = {f"s{i}": a for i, a in enumerate(group)}
    payload = [{**a.model_dump(mode="json"), "id": key} for key, a in lookup.items()]

    class Repair(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "repair_evidence":
                assert data["claims"][0]["supported_source_ids"] == ["s0"]
                return schema.model_validate_json(
                    json.dumps(
                        {
                            "repairs": [
                                {
                                    "claim_index": 0,
                                    "evidence": [
                                        {
                                            "source_id": "missing"
                                            if repair_kind == "unknown_id"
                                            else "s1",
                                            "quote": "Invented quote"
                                            if repair_kind == "wrong_quote"
                                            else group[1].snippet,
                                        }
                                    ],
                                }
                            ]
                        }
                    )
                )
            return await super().structured(stage, data, schema)

    run(
        repair_event_evidence(
            event,
            group,
            Repair(verify_support="partial" if repair_kind == "partial" else "full"),
            lookup,
            payload,
        )
    )
    assert len(event.claims[0].evidence) == expected_count
    assert event.claims[0].evidence[0].source_url == group[0].url


def test_repair_invalid_mapping_preserves_verified_evidence():
    from daybrief.pipeline import repair_event_evidence

    group = pair_articles()
    event = run(extract_event_claims(group, MockModel()))
    event.claims[0].evidence = event.claims[0].evidence[:1]
    before = event.model_dump()

    class Bad(MockModel):
        async def structured(self, stage, data, schema):
            if stage == "repair_evidence":
                return schema.model_validate_json(
                    '{"repairs": [{"claim_index": 999, "evidence": []}]}'
                )
            return await super().structured(stage, data, schema)

    run(
        repair_event_evidence(event, group, Bad(), {"s0": group[0], "s1": group[1]}, [])
    )
    assert event.model_dump() == before


def test_same_url_excerpt_preserved_without_new_source():
    from daybrief.pipeline import enrich_candidate_groups

    original = normalize_articles(pair_articles()[:1])
    extra = original[0].model_copy(
        update={"snippet": "Another passage from the same article."}
    )

    class Search:
        async def search_event_sources(self, *args):
            return [extra]

    result = run(
        enrich_candidate_groups(
            [original], "general", 24, NOW, Search(), MockModel(), 1, 2
        )
    )
    assert len(result[0]) == 1
    assert result[0][0].snippet.count(extra.snippet) == 1
    assert pair_articles()[0].snippet in result[0][0].snippet


def test_snapshot_search_miss_never_calls_provider(tmp_path, monkeypatch):
    from daybrief.replay import SnapshotSearch

    record = {
        "saved_at": 1,
        "request": {
            "query": "seed",
            "topic": "general",
            "hours": 24,
            "start_date": "2026-10-14",
        },
        "value": {"articles": [a.model_dump(mode="json") for a in pair_articles()]},
    }
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(record))

    async def forbidden(*args):
        raise AssertionError("Replay must not perform search network requests")

    monkeypatch.setattr("daybrief.adapters.post_json", forbidden)
    search = SnapshotSearch(Settings(), seed)
    assert len(run(search.search_news("general", 24, NOW))) == 2
    assert run(search._search("missing", "general", 24, NOW, 10)) == []
    assert search.misses == ["missing"]


@pytest.mark.parametrize("support,expected", [("full", 2), ("partial", 1)])
def test_matching_quote_expansion_requires_independent_verification(support, expected):
    from daybrief.pipeline import expand_matching_evidence

    group = pair_articles()
    event = run(extract_event_claims(group, MockModel()))
    event.claims = [event.claims[0]]
    event.claims[0].evidence = event.claims[0].evidence[:1]
    group[1].snippet = group[0].snippet
    run(expand_matching_evidence(event, group, MockModel(verify_support=support)))
    assert len(event.claims[0].evidence) == expected


def test_narration_keeps_context_notice_once():
    result = brief()
    assert result.narration.count("The longer-term effects remain uncertain") == 1
    assert "The longer-term effects remain uncertain" in result.closing
    assert 45 <= result.estimated_duration_seconds <= 75
