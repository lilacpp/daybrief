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
