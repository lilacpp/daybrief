"""Reference validation and saved-evidence binding shared by generation and video."""

import hashlib
import json

from .models import NarrativePlan


def validate_narrative_references(events, plan):
    if len(events) != 3 or len({event.id for event in events}) != 3:
        raise ValueError("Narrative requires three distinct events")
    if [segment.event_id for segment in plan.segments] != [
        event.id for event in events
    ]:
        raise ValueError("Invalid narrative event mapping or ordering")
    for event, segment in zip(events, plan.segments, strict=True):
        ids = segment.supporting_claim_ids
        if not 1 <= len(ids) <= 2 or len(ids) != len(set(ids)):
            raise ValueError("Invalid narrative claim references")
        lookup = {f"c{i}": claim for i, claim in enumerate(event.claims)}
        if any(claim_id not in lookup for claim_id in ids):
            raise ValueError("Unknown narrative claim reference")
        claims = [lookup[claim_id] for claim_id in ids]
        if any(
            c.support_status != "supported" or c.classification != "confirmed_fact"
            for c in claims
        ):
            raise ValueError("Unsafe narrative claim reference")
        if not any(c.is_core for c in claims):
            raise ValueError("Narrative segment requires a core claim")


def narrative_evidence_digest(events, plan):
    """Detect stored text/evidence edits; not authentication against database writers."""
    payload = {
        "version": 1,
        "plan": plan.model_dump(mode="json"),
        "events": [
            {
                "id": event.id,
                "sensitivity": event.sensitivity,
                "claims": [claim.model_dump(mode="json") for claim in event.claims],
            }
            for event in events
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def validate_saved_narrative(brief):
    plan = NarrativePlan(segments=brief.narrative_segments)
    validate_narrative_references(brief.selected_events, plan)
    if brief.narrative_evidence_digest != narrative_evidence_digest(
        brief.selected_events, plan
    ):
        raise ValueError("Narrative no longer matches validated verified claims")
    expected = "\n\n".join(
        [brief.opening, *(s.text for s in plan.segments), brief.closing]
    )
    if brief.narration != expected:
        raise ValueError("Narrative no longer matches validated verified claims")
    return [brief.opening, *(s.text for s in plan.segments), brief.closing]
