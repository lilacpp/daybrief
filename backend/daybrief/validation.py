"""Semantic model-output checks shared by cache and pipeline boundaries."""

import json
from collections import Counter


class ClusterMembershipError(ValueError):
    pass


def validate_cluster_membership(output, articles):
    expected = {a["id"] for a in articles}
    counts = Counter(article_id for group in output.groups for article_id in group)
    problems = {
        "missing_ids": sorted(expected - counts.keys()),
        "unknown_ids": sorted(counts.keys() - expected),
        "duplicate_ids": sorted(i for i, count in counts.items() if count > 1),
        "empty_groups": [i for i, group in enumerate(output.groups) if not group],
    }
    if any(problems.values()):
        raise ClusterMembershipError(
            "Invalid clustering: every article must appear exactly once; "
            + json.dumps(problems)
        )


class NarrativeVerifierMappingError(ValueError):
    pass


def validate_narrative_verifier_mapping(output, segments):
    """One verdict per input segment, never one verdict per supporting claim."""
    expected = {(segment["claim_index"], 0) for segment in segments}
    received = [(check.claim_index, check.evidence_index) for check in output.checks]
    counts = Counter(received)
    if len(received) != len(expected) or set(received) != expected:
        raise NarrativeVerifierMappingError(
            "narrative_verifier_mapping_failed: claim_index is the narrative segment "
            "index, not a supporting-claim index. Return exactly one check per "
            "input segment with evidence_index=0, regardless of how many claims "
            "support it. " + json.dumps({
                "expected_pairs": sorted(expected),
                "received_pairs": received,
                "missing_pairs": sorted(expected - counts.keys()),
                "unexpected_pairs": sorted(counts.keys() - expected),
                "duplicate_pairs": sorted(pair for pair, count in counts.items() if count > 1),
            })
        )
