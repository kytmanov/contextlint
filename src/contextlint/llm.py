"""The LLM seam — the only module that turns a grounding bundle into suggestions.

M1 ships the `agent` (emit) provider: no endpoint. The CLI writes self-contained task
bundles for gated files; a host Claude session (run as a skill) fills them in and the
results are read back via `--llm-results`. The HTTP (`openai`) and `bedrock-native`
providers are later milestones and slot in behind this same data contract.
"""

from __future__ import annotations

import json

from .models import Enforcement, Evidence, Finding, Severity, Snapshot, Source

# JSON schema (informal) a host agent must return per gated file:
#   { "<repo-relative file>": [ {message, severity, line?, replacement?,
#                                evidence:[{kind, detail, path?, commit?}]}, ... ] }
RESULT_SCHEMA_HINT = (
    "Return JSON mapping each file path to a list of suggestions. Each suggestion needs "
    "a `message`, a `severity` (info|low|medium|high), and at least one `evidence` item "
    "citing a concrete repo fact from the provided snapshot. Optionally include `line` "
    "and `replacement` text."
)


def build_task_bundle(file: str, text: str, deterministic: list[Finding],
                      snapshot: Snapshot, llm_rules: list[dict]) -> dict:
    """Self-contained context pack for one file: enough for the agent to reason grounded."""
    return {
        "file": file,
        "content": text,
        "known_findings": [f.to_dict() for f in deterministic],
        "snapshot": {
            "repo_id": snapshot.repo_id,
            "head": snapshot.head,
            "manifests": sorted(snapshot.manifests.keys()),
            "recently_deleted": dict(list(snapshot.deleted_paths.items())[:50]),
        },
        "rules": llm_rules,
        "instructions": RESULT_SCHEMA_HINT,
    }


def write_tasks(path: str, bundles: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"tasks": bundles, "schema": RESULT_SCHEMA_HINT}, fh, indent=2)


def load_results(path: str) -> dict[str, list[dict]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    # Accept either {file: [...]} or {"results": {file: [...]}}.
    return data.get("results", data) if isinstance(data, dict) else {}


def verify_suggestions(file: str, raw: list[dict], snapshot: Snapshot) -> list[Finding]:
    """Keep only suggestions that cite checkable evidence; drop the rest (anti-hallucination).

    A cited `path` must actually be missing/deleted (else the claim is wrong). Suggestions
    with no evidence at all are dropped. LLM findings are always advisory.
    """
    out: list[Finding] = []
    for s in raw:
        message = (s.get("message") or "").strip()
        ev_items = s.get("evidence") or []
        if not message or not ev_items:
            continue
        evidence: list[Evidence] = []
        ok = True
        for e in ev_items:
            path = e.get("path")
            if path is not None:
                clean = path.strip().lstrip("./")
                # If it claims a path fact, the path must really be absent from the repo.
                if snapshot.path_exists(clean) and clean not in snapshot.deleted_paths:
                    ok = False
                    break
            evidence.append(Evidence(
                kind=e.get("kind", "llm"),
                detail=e.get("detail", ""),
                path=e.get("path"),
                commit=e.get("commit"),
            ))
        if not ok or not evidence:
            continue
        try:
            severity = Severity(s.get("severity", "low"))
        except ValueError:
            severity = Severity.LOW
        out.append(Finding(
            rule_id="llm-suggestion",
            file=file,
            message=message,
            severity=severity,
            enforcement=Enforcement.ADVISORY,
            source=Source.LLM,
            evidence=evidence,
            matched_text=message,
            line=s.get("line"),
            replacement=s.get("replacement"),
            confidence=float(s.get("confidence", 0.6)),
        ))
    return out
