"""Expand the `@import` graph so the audit covers the real context surface.

Harnesses (Claude Code / Codex) load `@imports` recursively, up to a hop limit. A file
pulled in via `@import` is loaded into context every session, so it must be audited too —
even if no discovery glob matches it. This module walks that graph from the discovered
seed files and returns the full ordered set of existing files to audit, plus the text it
read (so the audit loop need not re-read the same files from disk).

Detection of *broken* imports lives in the `ref_resolves` rule, not here; this module only
follows imports whose target actually exists, using the same `IMPORT_RE` / masking /
`resolve_ref` as that rule so the two never disagree on what an import is.
"""

from __future__ import annotations

from typing import Callable

from .models import Snapshot
from .rules import IMPORT_RE, _mask_code_regions, resolve_ref

_MAX_DEPTH = 5  # matches the harness import-recursion limit


def expand_import_graph(
    snapshot: Snapshot,
    seed_files: list[str],
    read_text: Callable[[str], str | None],
) -> tuple[list[str], dict[str, str]]:
    """BFS from the seed context files over `@imports`, ≤5 hops from a seed.

    Returns (ordered unique files: seeds first then imported, text cache rel->text).
    Only files that exist in the snapshot are followed; cycles terminate via `seen`.
    """
    text_cache: dict[str, str] = {}
    order: list[str] = []
    seen: set[str] = set()
    queue: list[tuple[str, int]] = []

    for rel in seed_files:
        if rel not in seen:
            seen.add(rel)
            order.append(rel)
            queue.append((rel, 0))

    i = 0
    while i < len(queue):
        rel, depth = queue[i]
        i += 1
        text = read_text(rel)
        if text is None:
            continue
        text_cache[rel] = text
        if depth >= _MAX_DEPTH:
            continue
        masked = _mask_code_regions(text)
        for m in IMPORT_RE.finditer(masked):
            s, e = m.span(1)
            target = resolve_ref(rel, text[s:e])
            if target is None or target in seen or target not in snapshot.files:
                continue
            seen.add(target)
            order.append(target)
            queue.append((target, depth + 1))

    return order, text_cache
