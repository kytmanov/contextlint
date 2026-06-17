"""Per-repo health score and severity counts.

Deterministic and model-independent so the score is reproducible across engineers and
LLMs. This is the machine-readable signal a future fleet digest aggregates over.
"""

from __future__ import annotations

from .models import Finding, Severity

_WEIGHT = {
    Severity.HIGH: 15,
    Severity.MEDIUM: 7,
    Severity.LOW: 3,
    Severity.INFO: 1,
}


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts = {s.value: 0 for s in Severity}
    for f in findings:
        counts[f.severity.value] += 1
    return counts


def health_score(findings: list[Finding]) -> int:
    """100 = clean. Each finding subtracts a severity-weighted penalty; floor 0."""
    penalty = sum(_WEIGHT[f.severity] for f in findings)
    return max(0, 100 - penalty)
