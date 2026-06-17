"""Core data model shared across the pipeline.

Everything downstream depends on these types, so they are intentionally small and
self-contained. A `Finding` carries the evidence that grounds it and a content-based
signature that survives edits (used by the feedback loop in later milestones).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Enforcement(str, Enum):
    ADVISORY = "advisory"
    BLOCKING = "blocking"


class Source(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"


# Severity ordering for scoring / sorting (higher = worse).
SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
}


@dataclass
class Evidence:
    """A concrete repo fact backing a finding.

    `kind` is a stable machine tag (e.g. "path_deleted", "path_missing", "dead_link",
    "manifest_drift", "token_bloat"); `detail` is the human-readable explanation.
    """

    kind: str
    detail: str
    path: str | None = None
    line: int | None = None
    commit: str | None = None

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "detail": self.detail}
        if self.path is not None:
            d["path"] = self.path
        if self.line is not None:
            d["line"] = self.line
        if self.commit is not None:
            d["commit"] = self.commit
        return d


@dataclass
class Finding:
    """A single grounded issue in a context file."""

    rule_id: str
    file: str  # repo-relative path of the context file
    message: str
    severity: Severity
    enforcement: Enforcement
    source: Source
    evidence: list[Evidence] = field(default_factory=list)
    matched_text: str = ""
    line: int | None = None
    replacement: str | None = None  # proposed replacement text, when available
    confidence: float = 1.0

    @property
    def signature(self) -> str:
        """Stable, content-based id.

        Keyed on (rule, file, normalized matched text) — deliberately NOT the line
        number — so a rejection survives edits that shift lines around. This is what
        the shared feedback store keys on in a later milestone.
        """
        norm = " ".join(self.matched_text.split()).lower()
        raw = f"{self.rule_id}\x1f{self.file}\x1f{norm}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "rule_id": self.rule_id,
            "file": self.file,
            "message": self.message,
            "severity": self.severity.value,
            "enforcement": self.enforcement.value,
            "source": self.source.value,
            "line": self.line,
            "matched_text": self.matched_text,
            "replacement": self.replacement,
            "confidence": self.confidence,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class Rule:
    """A validation rule.

    Built-in checks ship as default rules in this same shape, so the engine is uniform
    and every check is tunable/disableable from config.
    """

    id: str
    description: str = ""
    match: str | None = None  # regex evaluated against the context file text
    assertion: str = "present"  # name of an assertion primitive (TOML key: `assert`)
    params: dict = field(default_factory=dict)
    severity: Severity = Severity.MEDIUM
    enforcement: Enforcement = Enforcement.ADVISORY
    message: str = ""
    scope: list[str] = field(default_factory=list)  # globs: which files this applies to
    ignore: list[str] = field(default_factory=list)  # captured values to skip
    enabled: bool = True
    locked: bool = False  # set by org baseline; repo config cannot disable (later milestone)


@dataclass
class Snapshot:
    """A factual picture of the repo, built once per run."""

    root: str
    repo_id: str
    files: set[str] = field(default_factory=set)  # repo-relative paths that exist now
    dirs: set[str] = field(default_factory=set)
    basenames: set[str] = field(default_factory=set)  # file basenames, for short-name refs
    manifests: dict[str, dict] = field(default_factory=dict)  # filename -> parsed data
    deleted_paths: dict[str, str] = field(default_factory=dict)  # path -> deleting commit
    renamed_paths: dict[str, str] = field(default_factory=dict)  # old path -> new path
    head: str | None = None

    def path_exists(self, rel: str) -> bool:
        rel = rel.strip().lstrip("./")
        return rel in self.files or rel in self.dirs
