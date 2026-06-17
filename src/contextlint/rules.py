"""The rule engine: evaluate declarative rules against a context file + snapshot.

A rule is a `match` regex plus an `assert` primitive. Primitives come in two modes:

* per-match  — the regex is run over the file and the primitive judges each captured
               value (e.g. does this referenced path exist?).
* per-file   — the primitive judges the whole file once (e.g. token budget).

Adding a primitive here is the only code change needed to extend validation; new *rules*
are pure config. Every primitive returns `Evidence` (a problem) or `None` (fine), so
findings are always grounded in a concrete fact.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Callable

from .models import Evidence, Finding, Rule, Snapshot, Source
from .tokens import approx_tokens

_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_PATHISH = re.compile(r"^[\w.\-]+\.[A-Za-z][A-Za-z0-9]+$")
_PLACEHOLDER = set("*?<>{}[]")


@dataclass
class Ctx:
    file: str  # repo-relative path of the context file being evaluated
    snapshot: Snapshot
    token_threshold: int


# --- helpers ---------------------------------------------------------------

def _clean_path(value: str) -> str:
    value = value.strip().strip("`").strip()
    value = value.split("#", 1)[0]  # drop anchors
    value = re.sub(r":\d+(:\d+)?$", "", value)  # drop :line(:col)
    return value.lstrip("./").rstrip("/")


def _looks_like_path(value: str) -> bool:
    if not value or " " in value or _URL_RE.match(value):
        return False
    # Globs/templates and home/abs/env paths are not repo-relative references.
    if any(c in _PLACEHOLDER for c in value) or value[0] in "~$/":
        return False
    return "/" in value or bool(_PATHISH.match(value))


def _missing_path_evidence(clean: str, snap: Snapshot, kind_prefix: str) -> Evidence:
    """Build the richest evidence available for a missing path."""
    if clean in snap.deleted_paths:
        sha = snap.deleted_paths[clean]
        return Evidence(
            kind="path_deleted",
            detail=f"`{clean}` was deleted in commit {sha[:10]}",
            path=clean,
            commit=sha,
        )
    if clean in snap.renamed_paths:
        return Evidence(
            kind="path_renamed",
            detail=f"`{clean}` was renamed to `{snap.renamed_paths[clean]}`",
            path=clean,
        )
    return Evidence(kind=f"{kind_prefix}_missing", detail=f"`{clean}` does not exist in the repo", path=clean)


# --- assertion primitives --------------------------------------------------
# Signature: (value, ctx, params) -> Evidence | None   (per-match)
#            (text,  ctx, params) -> Evidence | None   (per-file)

def _path_like_exists(value: str, ctx: Ctx, params: dict) -> Evidence | None:
    if not _looks_like_path(value):
        return None
    clean = _clean_path(value)
    if not clean or ctx.snapshot.path_exists(clean):
        return None
    # Git proof that this exact path was deleted/renamed is high signal — keep it.
    if clean in ctx.snapshot.deleted_paths or clean in ctx.snapshot.renamed_paths:
        return _missing_path_evidence(clean, ctx.snapshot, "path")
    # High-precision default: only report git-proven staleness. Docs legitimately mention
    # runtime paths, domains, and short names; flagging every "not in repo" token is noisy.
    if params.get("git_proven_only", False):
        return None
    # Looser mode: a bare/short name that exists deeper in the tree is not stale
    # (docs routinely cite `cli.py` for `src/pkg/cli.py`).
    if clean.rsplit("/", 1)[-1] in ctx.snapshot.basenames:
        return None
    return _missing_path_evidence(clean, ctx.snapshot, "path")


def _link_resolves(value: str, ctx: Ctx, params: dict) -> Evidence | None:
    target = value.strip()
    if not target or target.startswith("#") or _URL_RE.match(target) or target.startswith("mailto:"):
        return None
    target = target.split("#", 1)[0].strip()
    if not target:
        return None
    base = posixpath.dirname(ctx.file)
    resolved = posixpath.normpath(posixpath.join(base, target)).lstrip("./")
    if not resolved or resolved.startswith("..") or ctx.snapshot.path_exists(resolved):
        return None
    ev = _missing_path_evidence(resolved, ctx.snapshot, "link")
    if ev.kind == "link_missing":
        ev = Evidence(kind="dead_link", detail=f"link target `{target}` does not resolve", path=resolved)
    return ev


def _manifest_has(value: str, ctx: Ctx, params: dict) -> Evidence | None:
    needle = value.strip().strip("`")
    if not needle:
        return None
    for name, data in ctx.snapshot.manifests.items():
        if _contains_key_or_value(data, needle):
            return None
    if not ctx.snapshot.manifests:
        return None  # nothing to check against
    return Evidence(
        kind="manifest_drift",
        detail=f"`{needle}` is mentioned but not declared in any manifest",
    )


def _present(value: str, ctx: Ctx, params: dict) -> Evidence | None:
    return Evidence(kind="banned_pattern", detail=f"matches a disallowed pattern: `{value.strip()}`")


def _token_threshold(text: str, ctx: Ctx, params: dict) -> Evidence | None:
    threshold = int(params.get("threshold", ctx.token_threshold))
    count = approx_tokens(text)
    if count <= threshold:
        return None
    return Evidence(
        kind="token_bloat",
        detail=f"~{count} tokens (approx), over the {threshold} budget",
    )


def _absent(text: str, ctx: Ctx, params: dict, pattern: re.Pattern) -> Evidence | None:
    if pattern.search(text):
        return None
    return Evidence(kind="required_missing", detail="expected content is not present")


PER_MATCH: dict[str, Callable] = {
    "path_like_exists": _path_like_exists,
    "path_exists": _path_like_exists,
    "link_resolves": _link_resolves,
    "manifest_has": _manifest_has,
    "present": _present,
}
PER_FILE = {"token_threshold", "absent"}


def _contains_key_or_value(obj, needle: str) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == needle or _contains_key_or_value(v, needle):
                return True
    elif isinstance(obj, list):
        return any(_contains_key_or_value(v, needle) for v in obj)
    elif isinstance(obj, str):
        return obj == needle
    return False


# --- engine ----------------------------------------------------------------

def _in_scope(rule: Rule, file: str) -> bool:
    if not rule.scope:
        return True
    return any(fnmatch(file, pat) for pat in rule.scope)


def _ignored(rule: Rule, value: str) -> bool:
    return any(fnmatch(value, pat) for pat in rule.ignore)


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def evaluate_file(rules: list[Rule], file: str, text: str, snapshot: Snapshot,
                  token_threshold: int) -> list[Finding]:
    """Run every enabled, in-scope rule against one context file."""
    ctx = Ctx(file=file, snapshot=snapshot, token_threshold=token_threshold)
    findings: list[Finding] = []

    for rule in rules:
        if not rule.enabled or not _in_scope(rule, file):
            continue

        if rule.assertion in PER_FILE:
            ev = _eval_per_file(rule, text, ctx)
            if ev is not None:
                findings.append(_make_finding(rule, file, ev, matched_text="", line=None))
            continue

        primitive = PER_MATCH.get(rule.assertion)
        if primitive is None or not rule.match:
            continue
        try:
            pattern = re.compile(rule.match)
        except re.error:
            continue
        seen: set[str] = set()
        for m in pattern.finditer(text):
            value = m.group(1) if m.groups() else m.group(0)
            if _ignored(rule, value) or value in seen:
                continue
            seen.add(value)
            ev = primitive(value, ctx, rule.params)
            if ev is not None:
                findings.append(
                    _make_finding(rule, file, ev, matched_text=m.group(0),
                                  line=_line_of(text, m.start()))
                )
    return findings


def _eval_per_file(rule: Rule, text: str, ctx: Ctx) -> Evidence | None:
    if rule.assertion == "token_threshold":
        return _token_threshold(text, ctx, rule.params)
    if rule.assertion == "absent":
        if not rule.match:
            return None
        try:
            pattern = re.compile(rule.match)
        except re.error:
            return None
        return _absent(text, ctx, rule.params, pattern)
    return None


def _make_finding(rule: Rule, file: str, ev: Evidence, matched_text: str,
                  line: int | None) -> Finding:
    message = rule.message or ev.detail
    return Finding(
        rule_id=rule.id,
        file=file,
        message=message,
        severity=rule.severity,
        enforcement=rule.enforcement,
        source=Source.DETERMINISTIC,
        evidence=[ev],
        matched_text=matched_text,
        line=ev.line if ev.line is not None else line,
    )
