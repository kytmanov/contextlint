"""Render a run into a dated Markdown report + machine-readable JSON sidecar.

Output goes to an out-dir, never into the audited source tree.
"""

from __future__ import annotations

import json
import os
from datetime import date

from .analyze import FileAudit, RunResult
from .models import SEVERITY_RANK, Finding

_SEV_LABEL = {"high": "HIGH", "medium": "MED", "low": "LOW", "info": "INFO"}


def _trend(fa: FileAudit) -> str:
    delta = fa.token_delta
    if delta is None:
        return "new"
    if delta > 0:
        return f"+{delta} since last audit"
    if delta < 0:
        return f"{delta} since last audit"
    return "no change"


def _finding_card(f: Finding) -> list[str]:
    loc = f"`{f.file}`" + (f":{f.line}" if f.line else "")
    lines = [f"#### [{_SEV_LABEL.get(f.severity.value, f.severity.value.upper())}] "
             f"{f.rule_id} — {loc}",
             "",
             f"{f.message}  _(source: {f.source.value}, enforcement: {f.enforcement.value})_",
             ""]
    for ev in f.evidence:
        lines.append(f"- **Evidence:** {ev.detail}")
    if f.matched_text:
        snippet = f.matched_text.strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        lines.append(f"- **Matched:** `{snippet}`")
    if f.replacement:
        lines.append(f"- **Proposed:** {f.replacement}")
    lines.append("")
    return lines


def render_markdown(result: RunResult) -> str:
    counts = result.severity_counts
    lines: list[str] = [
        f"# ContextLint report — {result.repo_id}",
        "",
        f"- **Generated:** {result.generated_at}",
        f"- **HEAD:** {result.head or 'n/a'}",
        f"- **Provider:** {result.provider}"
        + (f" ({result.model})" if result.model else ""),
        "- **Health score:** n/a (no context files discovered)"
        if result.score is None else f"- **Health score:** {result.score}/100",
        f"- **Findings:** high {counts['high']} · medium {counts['medium']} · "
        f"low {counts['low']} · info {counts['info']}"
        + ("  · **BLOCKING present**" if result.has_blocking else ""),
        "",
    ]

    cat_scores = result.category_scores
    if cat_scores:
        rendered = " · ".join(f"{cat} {sc}" for cat, sc in sorted(cat_scores.items()))
        lines.insert(-1, f"- **By category:** {rendered}")

    total_findings = len(result.all_findings)
    if not result.files:
        lines += ["## No context files", "",
                  "No context files matched the configured globs — nothing was audited.", ""]
    elif total_findings == 0:
        lines += ["## Healthy", "",
                  "No grounded issues found in the discovered context files.", ""]

    for fa in result.files:
        flag = ""
        # bloat flag is driven by a token-bloat finding on this file
        if any(f.rule_id == "token-bloat" for f in fa.findings):
            flag = " ⚠️ bloat"
        gate = "deep analysis ran" if fa.llm_ran else (
            "pending deep analysis" if fa.llm_gated else "no material change — deep analysis skipped")
        lines.append(f"## `{fa.file}`")
        lines.append("")
        lines.append(f"- **Tokens (approx):** {fa.tokens} ({_trend(fa)}){flag}")
        lines.append(f"- **LLM pass:** {gate}")
        lines.append("")
        ordered = sorted(fa.findings, key=lambda f: -SEVERITY_RANK[f.severity])
        if not ordered:
            lines.append("_No findings._")
            lines.append("")
            continue
        for f in ordered:
            lines.extend(_finding_card(f))

    return "\n".join(lines).rstrip() + "\n"


def write_report(result: RunResult, out_dir: str, formats: list[str]) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    stamp = date.today().isoformat()
    written: dict[str, str] = {}

    if "md" in formats:
        md = render_markdown(result)
        dated = os.path.join(out_dir, f"report-{stamp}.md")
        with open(dated, "w", encoding="utf-8") as fh:
            fh.write(md)
        with open(os.path.join(out_dir, "latest.md"), "w", encoding="utf-8") as fh:
            fh.write(md)
        written["md"] = dated

    if "json" in formats:
        payload = json.dumps(result.to_dict(), indent=2)
        dated = os.path.join(out_dir, f"report-{stamp}.json")
        with open(dated, "w", encoding="utf-8") as fh:
            fh.write(payload)
        with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as fh:
            fh.write(payload)
        written["json"] = dated

    return written
