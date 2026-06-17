"""Orchestration: snapshot -> discover -> evaluate -> gate -> score.

Produces a `RunResult` (the report + JSON-sidecar source of truth) and the emit-mode
task bundles for files that warrant the gated LLM pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import llm, score, state
from .config import Config
from .discover import discover
from .imports import expand_import_graph
from .models import Enforcement, Finding, Snapshot
from .rules import evaluate_file
from .snapshot import build_snapshot
from .tokens import approx_tokens


def _read(root: str, rel: str) -> str | None:
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


@dataclass
class FileAudit:
    file: str
    tokens: int
    prev_tokens: int | None
    findings: list[Finding] = field(default_factory=list)
    llm_gated: bool = False  # the LLM pass was warranted for this file
    llm_ran: bool = False  # results were actually applied

    @property
    def token_delta(self) -> int | None:
        return None if self.prev_tokens is None else self.tokens - self.prev_tokens

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "tokens": self.tokens,
            "prev_tokens": self.prev_tokens,
            "token_delta": self.token_delta,
            "llm_gated": self.llm_gated,
            "llm_ran": self.llm_ran,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class RunResult:
    repo_id: str
    head: str | None
    provider: str
    model: str | None
    files: list[FileAudit]
    generated_at: str
    task_bundles: list[dict] = field(default_factory=list)

    @property
    def all_findings(self) -> list[Finding]:
        out: list[Finding] = []
        for fa in self.files:
            out.extend(fa.findings)
        return out

    @property
    def score(self) -> int | None:
        # None signals "nothing audited" — distinct from a real, clean 100. A fleet
        # digest can skip null instead of averaging in a fake perfect score.
        if not self.files:
            return None
        return score.health_score(self.all_findings)

    @property
    def severity_counts(self) -> dict[str, int]:
        return score.severity_counts(self.all_findings)

    @property
    def category_scores(self) -> dict[str, int] | None:
        # Parity with `score`: None when nothing was audited, not a fake set of 100s.
        if not self.files:
            return None
        return score.category_scores(self.all_findings)

    @property
    def has_blocking(self) -> bool:
        return any(f.enforcement is Enforcement.BLOCKING for f in self.all_findings)

    def to_dict(self) -> dict:
        return {
            "repo_id": self.repo_id,
            "head": self.head,
            "generated_at": self.generated_at,
            "provenance": {"provider": self.provider, "model": self.model},
            "score": self.score,
            "score_version": score.SCORE_VERSION,
            "category_scores": self.category_scores,
            "severity_counts": self.severity_counts,
            "has_blocking": self.has_blocking,
            "files": [fa.to_dict() for fa in self.files],
        }


def _should_run_llm(file: str, text: str, tokens: int, prior_files: dict,
                    token_threshold: int) -> bool:
    """Gate the expensive LLM pass: first sight, changed content, or over budget."""
    prior = prior_files.get(file)
    if prior is None:
        return True
    if prior.get("hash") != state.content_hash(text):
        return True
    return tokens > token_threshold


def audit_repo(root: str, cfg: Config, llm_results: dict | None = None,
               model: str | None = None) -> RunResult:
    snap: Snapshot = build_snapshot(root)
    seeds = discover(root, cfg.globs, cfg.out_dir)
    # Audit the real context surface: the glob-discovered seeds plus everything they pull
    # in via @import (the harness loads those into context too).
    files, text_cache = expand_import_graph(snap, seeds, lambda rel: _read(snap.root, rel))
    last_run = state.load_last_run(cfg.resolved_state_dir(), snap.repo_id)
    prior_files = last_run.get("files", {})

    audits: list[FileAudit] = []
    bundles: list[dict] = []
    new_state_files: dict = {}

    for rel in files:
        text = text_cache.get(rel)
        if text is None:
            text = _read(snap.root, rel)
        if text is None:
            continue

        tokens = approx_tokens(text)
        prev = prior_files.get(rel, {}).get("tokens")
        findings = evaluate_file(cfg.rules, rel, text, snap, cfg.token_threshold)
        gated = _should_run_llm(rel, text, tokens, prior_files, cfg.token_threshold)

        fa = FileAudit(file=rel, tokens=tokens, prev_tokens=prev,
                       findings=list(findings), llm_gated=gated)

        if gated:
            bundles.append(llm.build_task_bundle(rel, text, findings, snap, cfg.llm_rules))
        if llm_results and rel in llm_results:
            verified = llm.verify_suggestions(rel, llm_results[rel], snap)
            fa.findings.extend(verified)
            fa.llm_ran = True

        audits.append(fa)
        new_state_files[rel] = {"tokens": tokens, "hash": state.content_hash(text)}

    result = RunResult(
        repo_id=snap.repo_id,
        head=snap.head,
        provider=cfg.provider,
        model=model,
        files=audits,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        task_bundles=bundles,
    )

    state.save_run(cfg.resolved_state_dir(), snap.repo_id, {
        "head": snap.head,
        "generated_at": result.generated_at,
        "files": new_state_files,
    })
    return result
