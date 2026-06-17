"""Approximate token counting (stdlib only).

These counts are directional — good enough to flag bloat and show trend, NOT billing
grade. Exact counts need a model tokenizer (a dependency); that is a documented later
hook, and blocking token budgets must wait for it. See `count_exact` placeholder.
"""

from __future__ import annotations

import math
import re

_PIECE = re.compile(r"\w+|[^\w\s]")


def approx_tokens(text: str) -> int:
    """Estimate token count with a word/symbol heuristic.

    Word runs and standalone punctuation are counted, with long words scaled up to
    approximate sub-word (BPE) splitting (~4 chars per token within a run). This tracks
    real tokenizers closely enough for bloat detection and run-to-run trend.
    """
    total = 0
    for piece in _PIECE.findall(text):
        total += max(1, math.ceil(len(piece) / 4))
    return total


def count_exact(text: str) -> int | None:  # pragma: no cover - future hook
    """Placeholder for an exact tokenizer. Returns None until a tokenizer is wired in.

    Blocking token-budget rules should require this; until then they stay advisory.
    """
    return None
