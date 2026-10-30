import hashlib
import logging
import math
import re
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from .adapters import ModelOutputError, ProviderError
from .diagnostics import log_details
from .models import (
    Brief,
    Claim,
    ClaimEvidence,
    Clusters,
    EvidenceChecks,
    EvidenceRepairs,
    ExtractedEvent,
    NarrativePlan,
    NarrativeSelectionPlan,
    NewsEvent,
    Ranking,
    VerifiedEventSummary,
)
from .narrative import narrative_evidence_digest, validate_narrative_references
from .search_queries import general_enrichment_priority, macro_candidate_priority
from .validation import (
    ClusterMembershipError,
    NarrativeVerifierMappingError,
    validate_cluster_membership,
    validate_narrative_verifier_mapping,
)

logger = logging.getLogger("uvicorn.error")


# Remove only known invisible formatting characters; retain punctuation and wording.
_INVISIBLE_FORMATTING = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")


class SynthesisEligibilityError(ProviderError):
    pass


class SynthesisValidationError(ProviderError):
    pass


def normalize_text(text):
    text = text.translate(_INVISIBLE_FORMATTING)
    return re.sub(r"\s+", " ", text).strip().casefold()


def source_host(url):
    return (urlsplit(str(url)).hostname or "").lower().removeprefix("www.")


def format_stage_counts(counts):
    return ", ".join(f"{name}={value}" for name, value in counts.items())


def log_stage_counts(counts, stage):
    logger.info("Pipeline stage=%s: %s", stage, format_stage_counts(counts))


def normalize_articles(articles):
    result = {}
    for a in articles:
        u = urlsplit(str(a.url))
        host = (u.hostname or "").lower().removeprefix("www.")
        query = urlencode(
            sorted(
                (k, v)
                for k, v in parse_qsl(u.query)
                if not k.startswith("utm_") and k not in ("fbclid", "gclid")
            )
        )
        url = urlunsplit((u.scheme.lower(), host, u.path.rstrip("/") or "/", query, ""))
        a = a.model_copy(
            update={
                "id": hashlib.sha256(url.encode()).hexdigest(),
                "url": type(a.url)(url),
                "original_url": a.original_url or a.url,
                "publisher": a.publisher.strip().casefold(),
            }
        )
        if url not in result:
            result[url] = a
        elif a.snippet and normalize_text(a.snippet) not in normalize_text(
            result[url].snippet
        ):
            result[url].snippet += (
                "\n\n[Additional search excerpt from the same URL]\n" + a.snippet
            )
    return list(result.values())


def filter_recent_articles(articles, lookback_hours, now):
    return [
        a
        for a in articles
        if now - timedelta(hours=lookback_hours) <= a.published_at <= now
    ]


async def cluster_articles_into_events(articles, model):
    output = await model.structured(
        "cluster", [a.model_dump(mode="json") for a in articles], Clusters
    )
    log_details("clustering", articles=articles, groups=output.groups)
    try:
        validate_cluster_membership(
            output, [a.model_dump(mode="json") for a in articles]
        )
    except ClusterMembershipError as exc:
        raise ProviderError(str(exc)) from None
    lookup = {a.id: a for a in articles}
    return [[lookup[i] for i in group] for group in output.groups]


def snippet_contains_quote(snippet, quote):
    normalized_quote = normalize_text(quote)
    return bool(normalized_quote) and normalized_quote in normalize_text(snippet)


def filter_extractive_evidence(event, sources):
    lookup = {str(a.url): a for a in sources}
    rejected = {"duplicate": 0, "unknown_source": 0, "quote_not_in_snippet": 0}
    for claim_index, claim in enumerate(event.claims):
        kept = []
        seen = set()
        for item in claim.evidence:
            url = str(item.source_url)
            if url in seen:
                rejected["duplicate"] += 1
                log_details(
                    "evidence_rejected",
                    event_id=event.id,
                    claim_index=claim_index,
                    claim_text=claim.text,
                    evidence=item,
                    reason="duplicate",
                )
                continue
            if url not in lookup:
                rejected["unknown_source"] += 1
                log_details(
                    "evidence_rejected",
                    event_id=event.id,
                    claim_index=claim_index,
                    claim_text=claim.text,
                    evidence=item,
                    reason="unknown_source",
                )
                continue
            if not snippet_contains_quote(lookup[url].snippet, item.quote):
                rejected["quote_not_in_snippet"] += 1
                log_details(
                    "evidence_rejected",
                    event_id=event.id,
                    claim_index=claim_index,
                    claim_text=claim.text,
                    evidence=item,
                    reason="quote_not_in_snippet",
                )
                continue
            seen.add(url)
            kept.append(item)
        claim.evidence = kept
    logger.info("Evidence extraction event=%s rejected=%s", event.id, rejected)
    return event


async def judge_claim_evidence(event, sources, model):
    lookup = {str(a.url): a for a in sources}
    payload = [
        {
            "claim_index": claim_index,
            "evidence_index": evidence_index,
            "claim_text": claim.text,
            "source_url": str(item.source_url),
            "quote": item.quote,
            "snippet": lookup[str(item.source_url)].snippet,
        }
        for claim_index, claim in enumerate(event.claims)
        for evidence_index, item in enumerate(claim.evidence)
    ]
    if not payload:
        return event
    log_details("verification_input", event_id=event.id, pairs=payload)
    judged = await model.structured("verify", payload, EvidenceChecks)
    log_details("verification_output", event_id=event.id, checks=judged.checks)
    expected = {(row["claim_index"], row["evidence_index"]) for row in payload}
    got = {(check.claim_index, check.evidence_index) for check in judged.checks}
    if got != expected:
        raise ProviderError("Incomplete evidence verification")
    logger.info(
        "Evidence verification event=%s decisions=%s",
        event.id,
        {
            status: sum(c.support == status for c in judged.checks)
            for status in ("full", "partial", "none", "contradicted")
        },
    )
    allowed = {
        (check.claim_index, check.evidence_index)
        for check in judged.checks
        if check.support == "full"
    }
    for claim_index, claim in enumerate(event.claims):
        claim.evidence = [
            item
            for evidence_index, item in enumerate(claim.evidence)
            if (claim_index, evidence_index) in allowed
        ]
    return event


def unique_by(items, key):
    result = []
    seen = set()
    for item in items:
        value = key(item)
        if value in seen:
            continue
        seen.add(value)
        result.append(item)
    return result


def apply_claim_support_status(event, sources):
    for claim_index, claim in enumerate(event.claims):
        original_classification = claim.classification
        claim.evidence = unique_by(
            claim.evidence, key=lambda item: str(item.source_url)
        )
        claim.supporting_source_urls = [item.source_url for item in claim.evidence]
        independent = {source_host(url) for url in claim.supporting_source_urls}
        claim.support_status = (
            "supported"
            if len(independent) >= 2
            else "partially_supported"
            if independent
            else "unsupported"
        )
        if (
            claim.classification == "confirmed_fact"
            and claim.support_status != "supported"
        ):
            claim.classification = "reported_claim"
        reasons = []
        if not independent:
            reasons.append("no_valid_evidence")
        elif len(independent) < 2:
            reasons.append("single_hostname")
        if claim.classification != "confirmed_fact":
            reasons.append("classification_not_confirmed_fact")
        if not claim.is_core:
            reasons.append("not_core_fact")
        logger.info(
            "Claim support event=%s claim_index=%d original_classification=%s "
            "classification=%s valid_evidence=%d distinct_hostnames=%d "
            "support_status=%s eligible=%s reasons=%s",
            event.id,
            claim_index,
            original_classification,
            claim.classification,
            len(claim.evidence),
            len(independent),
            claim.support_status,
            not reasons,
            ",".join(reasons) or "none",
        )
    eligible_claims = sum(
        claim.is_core and claim.support_status == "supported" and claim.classification == "confirmed_fact"
        for claim in event.claims
    )
    logger.info(
        "Event support event=%s claims=%d eligible_claims=%d corroborated=%s reason=%s",
        event.id,
        len(event.claims),
        eligible_claims,
        bool(eligible_claims),
        "none"
        if eligible_claims
        else "no_claims"
        if not event.claims
        else "no_eligible_claim",
    )
    if len({source_host(a.url) for a in sources}) < 2:
        event.uncertainties.append(
            "Insufficiently corroborated: only one independent source."
        )
    event.what_happened = " ".join(
        claim.text
        for claim in event.claims
        if claim.support_status != "unsupported"
        and claim.classification in ("confirmed_fact", "reported_claim")
    )
    text = " ".join(a.snippet for a in sources).casefold()
    if any(
        word in text
        for word in ("death", "killed", "flood", "war", "violence", "disaster")
    ):
        event.sensitivity = "sensitive"
    for text in [event.headline, event.why_it_matters, *event.uncertainties]:
        enforce_editorial_tone(event, text)
    return event


def resolve_source_evidence(evidence, source_lookup):
    resolved = []
    for item in evidence:
        source = source_lookup.get(item.source_id)
        if source is None:
            log_details("evidence_rejected", evidence=item, reason="unknown_source_id")
            continue
        resolved.append(ClaimEvidence(source_url=source.url, quote=item.quote))
    return resolved


async def expand_matching_evidence(event, group, model):
    """Find cross-source verbatim matches the model may have omitted."""
    candidate = event.model_copy(deep=True)
    added = 0
    for original, pending in zip(event.claims, candidate.claims, strict=True):
        pending.evidence = []
        hosts = {source_host(item.source_url) for item in original.evidence}
        if len(hosts) >= 2:
            continue
        for source in group:
            if source_host(source.url) in hosts:
                continue
            quote = next(
                (
                    item.quote
                    for item in original.evidence
                    if snippet_contains_quote(source.snippet, item.quote)
                ),
                None,
            )
            if quote:
                pending.evidence.append(
                    ClaimEvidence(source_url=source.url, quote=quote)
                )
                added += 1
    if not added:
        return
    log_details("matching_evidence_candidates", event_id=event.id, event=candidate)
    try:
        await judge_claim_evidence(candidate, group, model)
        for original, pending in zip(event.claims, candidate.claims, strict=True):
            original.evidence = unique_by(
                original.evidence + pending.evidence,
                key=lambda item: str(item.source_url),
            )
    except ProviderError as exc:
        logger.warning(
            "Matching evidence verification failed event=%s: %s", event.id, exc
        )


async def repair_event_evidence(event, group, model, source_lookup, source_payload):
    if len({source_host(a.url) for a in group}) < 2:
        return
    targets = [
        {
            "claim_index": i,
            "text": claim.text,
            "supported_source_ids": [
                key
                for key, source in source_lookup.items()
                if source.url in {e.source_url for e in claim.evidence}
            ],
        }
        for i, claim in enumerate(event.claims)
        if claim.classification in ("confirmed_fact", "reported_claim")
        and len({source_host(e.source_url) for e in claim.evidence}) < 2
    ][:6]
    if not targets:
        return
    for target in targets:
        target["candidate_sources"] = [
            source
            for source in source_payload
            if source["id"] not in target["supported_source_ids"]
        ]
    payload = {"claims": targets}
    log_details("repair_input", event_id=event.id, **payload)
    try:
        output = await model.structured("repair_evidence", payload, EvidenceRepairs)
        log_details("repair_output", event_id=event.id, repairs=output.repairs)
        allowed = {target["claim_index"] for target in targets}
        indices = [repair.claim_index for repair in output.repairs]
        if len(indices) != len(set(indices)) or not set(indices) <= allowed:
            raise ProviderError("Invalid evidence repair claim mapping")
        candidate = event.model_copy(deep=True)
        for claim in candidate.claims:
            claim.evidence = []
        for repair in output.repairs:
            candidate.claims[repair.claim_index].evidence = resolve_source_evidence(
                repair.evidence, source_lookup
            )
        filter_extractive_evidence(candidate, group)
        await judge_claim_evidence(candidate, group, model)
        for original, repaired in zip(event.claims, candidate.claims, strict=True):
            original.evidence = unique_by(
                original.evidence + repaired.evidence,
                key=lambda item: str(item.source_url),
            )
        log_details("event_repaired", event_id=event.id, event=event)
    except ProviderError as exc:
        logger.warning(
            "Evidence repair failed event=%s; retaining verified evidence: %s",
            event.id,
            exc,
        )


def core_evidence_supported(event):
    return any(
        claim.is_core
        and claim.classification == "confirmed_fact"
        and len({source_host(e.source_url) for e in claim.evidence}) >= 2
        for claim in event.claims
    )


async def enrich_missing_claims(event, group, model, search, topic, hours, now, budget):
    search_claim = getattr(search, "search_claim_sources", None)
    if search_claim is None:
        return
    for attempt in range(budget):
        if core_evidence_supported(event):
            logger.info("Claim enrichment event=%s stopped=core_supported", event.id)
            break
        targets = [
            c for c in event.claims
            if c.is_core and c.classification == "confirmed_fact"
        ]
        if not targets:
            logger.info("Claim enrichment event=%s stopped=no_core_fact", event.id)
            break
        targets.sort(key=lambda c: len(c.evidence), reverse=True)
        claim = targets[attempt % len(targets)]
        try:
            retrieved = await search_claim(group, claim, topic, hours, now, attempt)
            recent = filter_recent_articles(normalize_articles(retrieved), hours, now)
            # Search hits are candidates only; require the same event before repair.
            merged = normalize_articles(group + recent)
            if not recent:
                continue
            regrouped = await cluster_articles_into_events(merged, model)
            seed_ids = {a.id for a in group}
            matched = next((g for g in regrouped if seed_ids <= {a.id for a in g}), None)
            log_details("claim_enrichment", event_id=event.id, claim=claim,
                        attempt=attempt + 1, retrieved=recent, accepted=matched)
            if matched is None:
                continue
            group[:] = matched
            lookup = {f"s{i}": a for i, a in enumerate(group)}
            payload = [{**a.model_dump(mode="json"), "id": key}
                       for key, a in lookup.items()]
            await expand_matching_evidence(event, group, model)
            await repair_event_evidence(event, group, model, lookup, payload)
            event.source_urls = [a.url for a in group]
            event.earliest_published_at = min(a.published_at for a in group)
            event.latest_published_at = max(a.published_at for a in group)
        except ProviderError as exc:
            logger.warning("Claim enrichment event=%s failed: %s", event.id, exc)


async def extract_event_claims(
    group, model, *, search=None, topic=None, hours=None, now=None, claim_budget=0
):
    source_lookup = {f"s{i}": article for i, article in enumerate(group)}
    source_payload = [
        {**a.model_dump(mode="json"), "id": key} for key, a in source_lookup.items()
    ]
    extracted = await model.structured("extract", source_payload, ExtractedEvent)
    event = NewsEvent(
        **extracted.model_dump(exclude={"claims"}),
        id=hashlib.sha256(
            "|".join(sorted(str(a.url) for a in group)).encode()
        ).hexdigest()[:16],
        claims=[
            Claim(
                **claim.model_dump(exclude={"evidence"}),
                evidence=resolve_source_evidence(claim.evidence, source_lookup),
                supporting_source_urls=[],
                support_status="unsupported",
            )
            for claim in extracted.claims
        ],
        source_urls=[a.url for a in group],
        earliest_published_at=min(a.published_at for a in group),
        latest_published_at=max(a.published_at for a in group),
        what_happened="",
        importance_score=0,
        selection_reason="",
    )
    log_details("event_extracted", event_id=event.id, sources=group, event=event)
    filter_extractive_evidence(event, group)
    log_details("event_quotes_filtered", event_id=event.id, event=event)
    await judge_claim_evidence(event, group, model)
    log_details("event_verified", event_id=event.id, event=event)
    await expand_matching_evidence(event, group, model)
    await repair_event_evidence(event, group, model, source_lookup, source_payload)
    if search is not None and claim_budget:
        await enrich_missing_claims(
            event, group, model, search, topic, hours, now, claim_budget
        )
    event = apply_claim_support_status(event, group)
    log_details(
        "event_final",
        event_id=event.id,
        event=event,
        corroborated=event_is_corroborated(event),
    )
    return event


async def synthesize_verified_event(event, model):
    # IDs refer to original claim positions; never renumber after filtering.
    eligible = {
        f"c{index}": claim
        for index, claim in enumerate(event.claims)
        if claim.support_status == "supported"
        and claim.classification == "confirmed_fact"
    }
    event.verified_summary = None
    if not any(claim.is_core for claim in eligible.values()):
        reason = "no_eligible_claims" if not eligible else "missing_core_claim"
        raise SynthesisEligibilityError(
            "Verified event synthesis requires a supported confirmed core claim "
            f"(reason={reason})"
        )
    summary = await model.structured(
        "verified_event_synthesis",
        {"claims": [
            {"id": claim_id, "text": claim.text, "is_core": claim.is_core}
            for claim_id, claim in eligible.items()
        ]},
        VerifiedEventSummary,
    )
    references = summary.supporting_claim_ids
    if (not references or len(references) != len(set(references))
            or any(claim_id not in eligible for claim_id in references)):
        raise SynthesisValidationError("Invalid verified synthesis claim references")
    # Recheck current claims, including on cached model results.
    referenced = [event.claims[int(claim_id[1:])] for claim_id in references]
    if any(claim.support_status != "supported" or claim.classification != "confirmed_fact"
           for claim in referenced):
        raise SynthesisValidationError("Unsafe verified synthesis claim references")
    if not any(claim.is_core for claim in referenced):
        raise SynthesisValidationError("Verified synthesis references require a core claim")
    for text in (summary.what_happened, summary.why_it_matters, summary.uncertainty):
        if text is not None:
            try:
                enforce_editorial_tone(event, text)
            except ProviderError as exc:
                raise SynthesisValidationError(str(exc)) from exc
    event.verified_summary = summary
    return event


def corroborated_event_count(events):
    return sum(1 for event in events if event_is_corroborated(event))


def event_is_corroborated(event):
    return any(
        claim.is_core and claim.support_status == "supported" and claim.classification == "confirmed_fact"
        for claim in event.claims
    )


async def rank_events(events, model, topic="general"):
    ranked = await model.structured(
        "rank",
        [{**e.model_dump(mode="json"), "edition_topic": topic} for e in events],
        Ranking,
    )
    if set(ranked.scores) != {e.id for e in events} or any(
        not 0 <= s <= 100 for s in ranked.scores.values()
    ):
        raise ProviderError("Invalid ranking")
    for e in events:
        e.importance_score = ranked.scores[e.id]
    return sorted(events, key=lambda e: (-e.importance_score, e.id))


def enforce_editorial_tone(event, text):
    if event.sensitivity != "normal" and re.search(
        r"\b(lol|hilarious|funny|joke|laugh|laughing|lmao)\b|!", text, re.IGNORECASE
    ):
        raise ProviderError("Sensitive story failed restrained-language checks")


def select_diverse_events(events, count=3, topic="general"):
    chosen = []
    categories = {}
    eligible = [event for event in events if event_is_corroborated(event)]
    for unique_pass in (True, False):
        for e in eligible:
            if e in chosen:
                continue
            n = categories.get(e.category, 0)
            if (unique_pass and n) or (topic == "general" and n >= 2):
                continue
            e.selection_reason = f"Selected for importance ({e.importance_score:.0f}/100), corroboration and category diversity."
            e.corroboration_level = "independently_corroborated"
            chosen.append(e)
            categories[e.category] = n + 1
            if len(chosen) == count:
                return chosen
    if chosen:
        return chosen
    raise ProviderError("No safely corroborated events available")


async def generate_brief(events, articles, topic, hours, now, model, mode):
    if not 1 <= len(events) <= 3:
        raise ProviderError("Brief requires one to three safe events")
    opening = {
        1: "This is DayBrief. One development.",
        2: "This is DayBrief. Two developments.",
        3: "This is DayBrief. Three developments.",
    }[len(events)]
    closing = "Thanks for watching."
    words_per_second = 2.3
    boilerplate_words = len(opening.split()) + len(closing.split())
    max_seconds = 25 * len(events)
    max_words = math.floor(max_seconds * words_per_second)
    inputs = []
    for event in events:
        if event.verified_summary is None:
            raise ProviderError("Narrative requires verified event summaries")
        inputs.append({
            "id": event.id,
            "claims": [
                {"id": f"c{i}", "text": c.text, "is_core": c.is_core}
                for i, c in enumerate(event.claims)
                if c.support_status == "supported" and c.classification == "confirmed_fact"
            ],
        })
    selection = await model.structured(
        "script_select", {"events": inputs}, NarrativeSelectionPlan,
    )
    try:
        validate_narrative_references(events, selection)
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    # Freeze references before writing; the writer cannot redefine its evidence.
    selected_ids = tuple(tuple(s.supporting_claim_ids) for s in selection.segments)
    selected_inputs = []
    for event, ids in zip(events, selected_ids, strict=True):
        selected_inputs.append({
            "id": event.id,
            "supporting_claim_ids": list(ids),
            "claims": [
                {"id": key, "text": event.claims[int(key[1:])].text,
                 "is_core": event.claims[int(key[1:])].is_core}
                for key in ids
            ],
        })
    # Soft targets follow the selected evidence, with at most three connective words.
    # No lower bound: sparse evidence should produce a shorter grounded briefing.
    max_segment_words = max_words - boilerplate_words
    per_event = []
    for event in selected_inputs:
        evidence_words = sum(len(claim["text"].split()) for claim in event["claims"])
        allowance = min(3, math.ceil(evidence_words * 0.1))
        per_event.append({
            "event_id": event["id"],
            "selected_evidence_words": evidence_words,
            "target_words": min(evidence_words + allowance, max_segment_words // len(events)),
        })
    target_segment_words = sum(event["target_words"] for event in per_event)
    plan = await model.structured(
        "script",
        {"events": selected_inputs, "word_budget": {
            "target_total_words": target_segment_words + boilerplate_words,
            "max_total_words": max_words,
            "boilerplate_words": boilerplate_words,
            "target_segment_words": target_segment_words,
            "per_event": per_event,
            "max_segment_words": max_segment_words,
        }},
        NarrativePlan,
    )
    try:
        validate_narrative_references(events, plan)
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    if tuple(tuple(s.supporting_claim_ids) for s in plan.segments) != selected_ids:
        raise ProviderError("Narrative references changed after claim selection")
    checks_input = []
    for index, (event, segment) in enumerate(zip(events, plan.segments, strict=True)):
        enforce_editorial_tone(event, segment.text)
        lookup = {f"c{i}": claim for i, claim in enumerate(event.claims)}
        checks_input.append({
            "claim_index": index, "evidence_index": 0, "text": segment.text,
            "claims": [{"id": key, "text": lookup[key].text, "is_core": lookup[key].is_core}
                       for key in segment.supporting_claim_ids],
        })
    judged = await model.structured("narrative_verify", checks_input, EvidenceChecks)
    # Defense in depth for adapters/mocks that bypass structured-output validation.
    try:
        validate_narrative_verifier_mapping(judged, checks_input)
    except NarrativeVerifierMappingError as exc:
        logger.error("%s", exc)
        raise ProviderError(str(exc)) from None
    if any(c.support != "full" for c in judged.checks):
        logger.error(
            "narrative_grounding_failed: segment_verdicts=%s",
            [(c.claim_index, c.support) for c in judged.checks],
        )
        raise ProviderError("narrative_grounding_failed: Narrative failed grounded evidence verification")
    # Revalidate after verification, including when model results came from cache.
    try:
        validate_narrative_references(events, plan)
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    narration = "\n\n".join([opening, *(s.text for s in plan.segments), closing])
    word_count = len(narration.split())
    duration = round(word_count / words_per_second, 1)
    if word_count > max_words or duration > max_seconds:
        raise ProviderError(f"Script exceeds the maximum {max_seconds} second narration limit")
    return Brief(
        id=str(uuid4()),
        topic=topic,
        generated_at=now,
        lookback_hours=hours,
        selected_events=events,
        sources=[
            a for a in articles if a.url in {u for e in events for u in e.source_urls}
        ],
        coverage_note=(
            f"Only {len(events)} safely corroborated stories were available; "
            "this edition is shorter to preserve evidence quality."
            if len(events) < 3 else None
        ),
        opening=opening,
        narration=narration,
        narrative_segments=plan.segments,
        narrative_evidence_digest=narrative_evidence_digest(events, plan),
        closing=closing,
        estimated_duration_seconds=duration,
        mode=mode,
    )


async def enrich_candidate_groups(
    groups,
    topic,
    hours,
    now,
    search,
    model,
    max_candidates,
    queries_per_candidate,
    max_fallback_candidates=6,
    max_fallback_articles=40,
):
    search_event = getattr(search, "search_event_sources", None)
    if search_event is None or not max_candidates or not queries_per_candidate:
        return groups
    # For the macro edition, spend the limited budget on concrete news first.
    owned_urls = {str(a.url) for group in groups for a in group}
    enriched = [list(group) for group in groups]
    fallback_pool = {}
    original_positions = {
        str(a.url): index for index, group in enumerate(groups) for a in group
    }
    candidates = [
        i
        for i, group in enumerate(groups)
        if len({source_host(a.url) for a in group}) < 3
    ]
    if topic == "markets":
        candidates = [i for i in candidates if not macro_candidate_priority(groups[i])]
        candidates.sort(key=lambda i: macro_candidate_priority(groups[i]))
        log_details(
            "macro_enrichment_priority",
            candidates=[
                {"cluster": i, "deferred_background": bool(macro_candidate_priority(groups[i]))}
                for i in candidates
            ],
        )
    if topic == "general":
        candidates.sort(key=lambda i: general_enrichment_priority(groups[i]))
    candidates = candidates[:max_candidates]
    for index in candidates:
        for attempt in range(queries_per_candidate):
            group = enriched[index]
            try:
                retrieved = await search_event(group, topic, hours, now, attempt)
                recent = filter_recent_articles(
                    normalize_articles(retrieved), hours, now
                )
                by_url = {str(a.url): a for a in recent}
                for article in group:
                    update = by_url.get(str(article.url))
                    if (
                        update
                        and update.snippet
                        and normalize_text(update.snippet)
                        not in normalize_text(article.snippet)
                    ):
                        article.snippet += (
                            "\n\n[Additional search excerpt from the same URL]\n"
                            + update.snippet
                        )
                        log_details("source_excerpt_added", article=article)
                fresh = [a for a in recent if str(a.url) not in owned_urls]
                if max_fallback_candidates:
                    for article in fresh:
                        url = str(article.url)
                        if url in fallback_pool:
                            fallback_pool[url] = normalize_articles(
                                [fallback_pool[url], article]
                            )[0]
                        elif len(fallback_pool) < max_fallback_articles:
                            fallback_pool[url] = article
                log_details(
                    "enrichment_results",
                    cluster_index=index,
                    attempt=attempt + 1,
                    seeds=group,
                    retrieved=retrieved,
                    recent=recent,
                    fresh=fresh,
                )
                if not fresh:
                    logger.info(
                        "Enrichment cluster=%d attempt=%d added=0", index, attempt + 1
                    )
                    continue
                # Reuse validated event clustering: related search hits are not evidence
                # until the model places them with every original member of this event.
                regrouped = await cluster_articles_into_events(group + fresh, model)
                seed_ids = {a.id for a in group}
                matched = next(
                    (g for g in regrouped if seed_ids <= {a.id for a in g}), None
                )
                log_details(
                    "enrichment_matching",
                    cluster_index=index,
                    attempt=attempt + 1,
                    groups=regrouped,
                    accepted_group=matched,
                )
                if matched is None:
                    logger.info("Enrichment cluster=%d rejected=split_seed", index)
                    continue
                added = len(matched) - len(group)
                enriched[index] = matched
                owned_urls.update(str(a.url) for a in matched)
                for article in matched:
                    fallback_pool.pop(str(article.url), None)
                logger.info(
                    "Enrichment cluster=%d attempt=%d retrieved=%d recent=%d added=%d hosts=%d",
                    index,
                    attempt + 1,
                    len(retrieved),
                    len(recent),
                    added,
                    len({source_host(a.url) for a in matched}),
                )
                if topic != "markets" and len({source_host(a.url) for a in matched}) >= 3:
                    break
            except ProviderError as exc:
                # Supplemental retrieval is best effort; original evidence is retained.
                logger.warning(
                    "Enrichment cluster=%d attempt=%d failed: %s",
                    index,
                    attempt + 1,
                    exc,
                )
    if fallback_pool:
        try:
            # Reconcile against ALL retained candidates, so a rediscovered event
            # cannot become a second selected story under another headline.
            combined = [a for group in enriched for a in group] + list(
                fallback_pool.values()
            )
            regrouped = await cluster_articles_into_events(combined, model)
            partitions = [{str(a.url) for a in group} for group in regrouped]
            if any(
                not any(
                    {str(a.url) for a in seed} <= partition for partition in partitions
                )
                for seed in enriched
            ):
                raise ProviderError("Fallback reconciliation split an existing event")
            primary = [
                group
                for group in regrouped
                if any(str(a.url) in original_positions for a in group)
            ]
            primary.sort(
                key=lambda group: min(
                    original_positions[str(a.url)]
                    for a in group
                    if str(a.url) in original_positions
                )
            )
            fallback = [
                group
                for group in regrouped
                if not any(str(a.url) in original_positions for a in group)
                and len({source_host(a.url) for a in group}) >= 2
            ]
            fallback.sort(
                key=lambda group: (
                    -len({source_host(a.url) for a in group}),
                    -len(group),
                )
            )
            fallback = fallback[:max_fallback_candidates]
            log_details(
                "fallback_candidates", primary_groups=primary, fallback_groups=fallback
            )
            logger.info(
                "Fallback pool: articles=%d primary_events=%d reserve_events=%d",
                len(fallback_pool),
                len(primary),
                len(fallback),
            )
            return primary + fallback
        except ProviderError as exc:
            logger.warning(
                "Fallback reconciliation failed; retaining original candidates: %s", exc
            )
    return enriched


def has_selectable_events(events, topic):
    eligible = [event for event in events if event_is_corroborated(event)]
    if topic != "general":
        return len(eligible) >= 3
    categories = {}
    for event in eligible:
        categories[event.category] = categories.get(event.category, 0) + 1
    return sum(min(count, 2) for count in categories.values()) >= 3


async def run_pipeline(
    topic,
    hours,
    now,
    search,
    model,
    mode,
    *,
    enrichment_max_candidates=6,
    enrichment_queries_per_candidate=2,
    fallback_max_candidates=6,
    fallback_max_articles=40,
):
    retrieved = await search.search_news(topic, hours, now)
    normalized = normalize_articles(retrieved)
    articles = filter_recent_articles(normalized, hours, now)
    log_details(
        "discovery",
        topic=topic,
        lookback_hours=hours,
        now=now,
        retrieved=retrieved,
        normalized=normalized,
        filtered=articles,
    )
    counts = {
        "retrieved": len(retrieved),
        "filtered": len(articles),
        "clusters": 0,
        "events": 0,
        "corroborated": 0,
        "selected": 0,
        "extraction_failed": 0,
        "synthesis_eligibility_skipped": 0,
        "synthesis_provider_failed": 0,
        "synthesis_schema_failed": 0,
        "synthesis_validation_failed": 0,
    }
    logger.info(
        "Article filtering: duplicates=%d expired=%d future=%d",
        len(retrieved) - len(normalized),
        sum(a.published_at < now - timedelta(hours=hours) for a in normalized),
        sum(a.published_at > now for a in normalized),
    )
    log_stage_counts(counts, "filtered")
    if not articles:
        raise ProviderError(
            "No recent articles with valid publication timestamps "
            f"({format_stage_counts(counts)})"
        )
    stage = "cluster"
    try:
        groups = await cluster_articles_into_events(articles, model)
        counts["clusters"] = len(groups)
        log_stage_counts(counts, "clustered; extraction_not_started")
        logger.info("Cluster sizes: %s", [len(g) for g in groups])
        primary_urls = {str(a.url) for a in articles}
        stage = "enrich"
        groups = await enrich_candidate_groups(
            groups,
            topic,
            hours,
            now,
            search,
            model,
            enrichment_max_candidates,
            min(1, enrichment_queries_per_candidate) if topic == "markets" else enrichment_queries_per_candidate,
            fallback_max_candidates,
            fallback_max_articles,
        )
        counts["clusters"] = len(groups)
        enriched_articles = [a for group in groups for a in group]
        logger.info(
            "Enrichment complete: articles_before=%d articles_after=%d cluster_sizes=%s",
            len(articles),
            len(enriched_articles),
            [len(g) for g in groups],
        )
        articles = enriched_articles
        events = []
        claim_candidates = 0
        if topic == "markets":
            groups.sort(key=macro_candidate_priority)
        stage = "extract"
        for index, group in enumerate(groups):
            is_fallback = not any(str(a.url) in primary_urls for a in group)
            if is_fallback:
                if has_selectable_events(events, topic):
                    logger.info(
                        "Skipping remaining reserve events: three selectable events already available"
                    )
                    break
                logger.info("Promoting reserve event cluster=%d", index + 1)
            logger.info(
                "Extracting cluster=%d/%d articles=%d",
                index + 1,
                len(groups),
                len(group),
            )
            log_details("event_input", cluster_index=index, sources=group)
            candidate_stage = "extraction"
            try:
                claim_budget = 0
                if (topic == "markets" and not macro_candidate_priority(group)
                        and claim_candidates < enrichment_max_candidates):
                    claim_budget = max(0, enrichment_queries_per_candidate - 1)
                    claim_candidates += 1
                event = await extract_event_claims(
                    group, model, search=search, topic=topic, hours=hours, now=now,
                    claim_budget=claim_budget,
                )
                candidate_stage = "synthesis"
                await synthesize_verified_event(event, model)
                events.append(event)
            except ProviderError as exc:
                if candidate_stage == "extraction":
                    reason = "extraction_failed"
                elif isinstance(exc, SynthesisEligibilityError):
                    reason = "synthesis_eligibility_skipped"
                elif isinstance(exc, ModelOutputError):
                    reason = "synthesis_schema_failed"
                elif isinstance(exc, SynthesisValidationError):
                    reason = "synthesis_validation_failed"
                else:
                    reason = "synthesis_provider_failed"
                counts[reason] += 1
                logger.warning(
                    "Skipping cluster=%d/%d: %s", index + 1, len(groups), exc
                )
                if candidate_stage == "synthesis":
                    logger.warning(
                        "Synthesis event=%s headline=%r reason=%s: %s",
                        event.id, event.headline, reason, exc,
                    )
                log_stage_counts(counts, reason)
                continue
            counts["events"] = len(events)
            counts["corroborated"] = corroborated_event_count(events)
            log_stage_counts(counts, "extracting")
        log_stage_counts(counts, "extracted")
        articles = normalize_articles([a for group in groups for a in group])
        if not counts["corroborated"]:
            stage = "select"
            raise ProviderError(
                "No safely corroborated events available"
            )
        stage = "rank"
        ranked = await rank_events(events, model, topic=topic)
        stage = "select"
        selected = select_diverse_events(ranked, topic=topic)
        counts["selected"] = len(selected)
        log_stage_counts(counts, "selected")
        log_details("selected_events", events=selected)
        stage = "script"
        return await generate_brief(selected, articles, topic, hours, now, model, mode)
    except ProviderError as exc:
        logger.error("Pipeline failed stage=%s: %s", stage, exc)
        log_stage_counts(counts, f"failed:{stage}")
        raise ProviderError(
            f"{exc} (stage={stage}; {format_stage_counts(counts)})"
        ) from None
