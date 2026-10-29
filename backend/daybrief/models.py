from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

Topic = Literal["general", "ai_technology", "science", "markets"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Article(StrictModel):
    id: str
    title: str
    url: HttpUrl
    original_url: HttpUrl | None = None
    publisher: str
    published_at: AwareDatetime
    retrieved_at: AwareDatetime
    snippet: str
    topic: Topic


class ClaimEvidence(StrictModel):
    source_url: HttpUrl
    quote: str = Field(min_length=1, max_length=500)


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    is_core: bool = True
    classification: Literal["confirmed_fact", "reported_claim", "analysis", "uncertain"]
    supporting_source_urls: list[HttpUrl]
    evidence: list[ClaimEvidence]
    support_status: Literal["supported", "partially_supported", "unsupported"]


class SourceEvidence(StrictModel):
    source_id: str
    quote: str = Field(min_length=1, max_length=500)


class ClaimRepair(StrictModel):
    claim_index: int = Field(ge=0)
    evidence: list[SourceEvidence]


class EvidenceRepairs(StrictModel):
    repairs: list[ClaimRepair]


class ExtractedClaim(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    is_core: bool = True
    classification: Literal["confirmed_fact", "reported_claim", "analysis", "uncertain"]
    evidence: list[SourceEvidence]


class ExtractedEvent(StrictModel):
    headline: str
    category: str
    why_it_matters: str
    uncertainties: list[str]
    claims: list[ExtractedClaim]
    sensitivity: Literal["normal", "sensitive", "critical"]


class VerifiedEventSummary(StrictModel):
    what_happened: str = Field(min_length=1, max_length=1000)
    # Analysis is separate from factual event synthesis; omit when ungrounded.
    why_it_matters: str | None = Field(default=None, min_length=1, max_length=1000)
    uncertainty: str | None = Field(default=None, min_length=1, max_length=1000)
    supporting_claim_ids: list[str] = Field(min_length=1)


class NewsEvent(StrictModel):
    id: str
    headline: str
    category: str
    what_happened: str
    why_it_matters: str
    uncertainties: list[str]
    claims: list[Claim]
    source_urls: list[HttpUrl]
    earliest_published_at: AwareDatetime
    latest_published_at: AwareDatetime
    importance_score: float = Field(ge=0, le=100)
    sensitivity: Literal["normal", "sensitive", "critical"]
    selection_reason: str
    verified_summary: VerifiedEventSummary | None = None


class Brief(StrictModel):
    id: str
    topic: Topic
    generated_at: AwareDatetime
    lookback_hours: int
    selected_events: list[NewsEvent] = Field(min_length=3, max_length=3)
    sources: list[Article]
    opening: str
    narration: str
    closing: str
    estimated_duration_seconds: float
    mode: Literal["mock", "real"]


class GenerateRequest(StrictModel):
    topic: Topic
    lookback_hours: int | None = Field(default=None, ge=1, le=168)


class Clusters(StrictModel):
    groups: list[list[str]]


class Ranking(StrictModel):
    scores: dict[str, float]


class ScriptPlan(StrictModel):
    claim_indices: dict[str, list[int]]


class EvidenceCheck(StrictModel):
    claim_index: int
    evidence_index: int
    support: Literal["full", "partial", "none", "contradicted"]


class EvidenceChecks(StrictModel):
    checks: list[EvidenceCheck]
