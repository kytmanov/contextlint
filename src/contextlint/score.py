"""Per-repo health score, per-category sub-scores, and severity counts.

Deterministic and model-independent so the score is reproducible across engineers and
LLMs. This is the machine-readable signal a future fleet digest aggregates over.

Score model v2: findings carry a `category` (reference | size | policy). Each category's
severity-weighted penalty is capped, so one noisy dimension can't flatten the headline
number to 0; the per-category sub-scores keep the detail. `SCORE_VERSION` marks the break
from v1 (a flat uncapped sum) for any downstream/fleet consumer.
"""

from __future__ import annotations

from .models import Finding, Severity

SCORE_VERSION = 2

_WEIGHT = {
    Severity.HIGH: 15,
    Severity.MEDIUM: 7,
    Severity.LOW: 3,
    Severity.INFO: 1,
}

# No single category may subtract more than this from the headline score.
_CATEGORY_CAP = 40


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts = {s.value: 0 for s in Severity}
    for f in findings:
        counts[f.severity.value] += 1
    return counts


def _category_penalties(findings: list[Finding]) -> dict[str, int]:
    raw: dict[str, int] = {}
    for f in findings:
        raw[f.category] = raw.get(f.category, 0) + _WEIGHT[f.severity]
    return {cat: min(_CATEGORY_CAP, pen) for cat, pen in raw.items()}


def category_scores(findings: list[Finding]) -> dict[str, int]:
    """0..100 health per scoring dimension that has findings."""
    return {cat: max(0, 100 - pen) for cat, pen in _category_penalties(findings).items()}


def health_score(findings: list[Finding]) -> int:
    """100 = clean. Sum of per-category capped penalties, subtracted from 100; floor 0."""
    penalty = sum(_category_penalties(findings).values())
    return max(0, 100 - penalty)
