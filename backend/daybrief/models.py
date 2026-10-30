from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from .anchors import AnchorId

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
    corroboration_level: Literal["independently_corroborated", "unverified"] = "unverified"


class NarrativeClaimSelection(StrictModel):
    event_id: str
    supporting_claim_ids: list[str] = Field(min_length=1, max_length=2)


class NarrativeSelectionPlan(StrictModel):
    segments: list[NarrativeClaimSelection] = Field(min_length=1, max_length=3)


class NarrativeSegment(StrictModel):
    event_id: str
    text: str = Field(min_length=1, max_length=1500)
    supporting_claim_ids: list[str] = Field(min_length=1, max_length=2)

    @field_validator("text")
    @classmethod
    def single_spoken_paragraph(cls, text):
        if not text.strip() or any(ord(c) < 32 for c in text):
            raise ValueError("Narrative must be one nonempty spoken paragraph")
        return text.strip()


class NarrativePlan(StrictModel):
    segments: list[NarrativeSegment] = Field(min_length=1, max_length=3)


class Brief(StrictModel):
    id: str
    topic: Topic
    generated_at: AwareDatetime
    lookback_hours: int
    selected_events: list[NewsEvent] = Field(min_length=1, max_length=3)
    sources: list[Article]
    coverage_note: str | None = None
    opening: str
    narration: str
    narrative_segments: list[NarrativeSegment] | None = None
    # Application-generated integrity binding, not a model self-attestation.
    narrative_evidence_digest: str | None = None
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


class EvidenceCheck(StrictModel):
    claim_index: int
    evidence_index: int
    support: Literal["full", "partial", "none", "contradicted"]


class EvidenceChecks(StrictModel):
    checks: list[EvidenceCheck]


class VisualAsset(StrictModel):
    type: Literal["article_image"]
    url: str | None = None
    local_path: str | None = None
    source_url: str | None = None
    publisher: str | None = None
    attribution: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)


class AnchorAsset(StrictModel):
    type: Literal["cat_anchor", "cartoon_anchor"] = "cartoon_anchor"
    character_id: AnchorId = "cat"
    image_path: str
    style_id: str
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class VisualScene(StrictModel):
    kind: Literal["intro", "story", "outro"]
    event_id: str | None = None
    narration_text: str
    visual_mode: Literal["article_with_anchor", "anchor_fullscreen"]
    article_image: VisualAsset | None = None
    anchor_asset: AnchorAsset
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    duration_source: Literal["audio_manifest", "estimated_from_text"]
    motion: Literal["slow_zoom", "gentle_pan", "anchor_breathe"]
    transition: Literal["crossfade"] = "crossfade"

    @model_validator(mode="after")
    def consistent_layout(self):
        if (self.kind == "story") != (self.event_id is not None):
            raise ValueError("Only story scenes have event IDs")
        if self.visual_mode == "article_with_anchor":
            if self.kind != "story" or self.article_image is None or self.article_image.type != "article_image":
                raise ValueError("Article layouts require a story and article image")
            if self.motion == "anchor_breathe":
                raise ValueError("Article layouts require article motion")
        elif self.article_image is not None or self.motion != "anchor_breathe":
            raise ValueError("Fullscreen anchor scenes do not have an article image")
        return self


class VisualPlan(StrictModel):
    brief_id: str
    brief_fingerprint: str
    created_at: AwareDatetime
    audio_job_id: str | None = None
    width: Literal[1280] = 1280
    height: Literal[720] = 720
    fps: Literal[30] = 30
    scenes: list[VisualScene] = Field(min_length=3, max_length=5)


class VisualPlanRequest(StrictModel):
    # Planning never invokes speech or image-generation providers.
    anchor: AnchorId = "cat"
