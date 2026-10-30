"""Brief-level cache identity and offline eligibility checks."""

import json
from datetime import timedelta

from .models import Brief
from .narrative import validate_saved_narrative
from .pipeline import event_is_corroborated


def brief_cache_key(topic, lookback_hours, mode):
    # Add future content-affecting fields (e.g. language) here; never visual state.
    return json.dumps({"version": 1, "content": {"topic": topic,
        "lookback_hours": lookback_hours, "mode": mode}}, sort_keys=True, separators=(",", ":"))


def usable_cached_brief(brief):
    try:
        # Revalidate even model_construct/model_copy payloads before admitting them.
        brief = Brief.model_validate_json(brief.model_dump_json())
        if len(brief.selected_events) < 2:
            return False
        if any(e.verified_summary is None or not event_is_corroborated(e) for e in brief.selected_events):
            return False
        for event in brief.selected_events:
            eligible = {f"c{i}": c for i, c in enumerate(event.claims)
                        if c.support_status == "supported" and c.classification == "confirmed_fact"}
            ids = event.verified_summary.supporting_claim_ids
            if (not ids or len(ids) != len(set(ids)) or not set(ids) <= eligible.keys()
                    or not any(eligible[key].is_core for key in ids)):
                return False
        validate_saved_narrative(brief)
    except (ValueError, TypeError):
        return False
    return True


def cache_expiry(brief, ttl_seconds):
    return brief.generated_at + timedelta(seconds=ttl_seconds)
