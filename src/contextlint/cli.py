"""`ctxlint` command-line entrypoint.

Exit codes (CI-friendly): 0 healthy, 1 advisory findings, 2 error, 3 blocking violation.
Output is written to an out-dir; the audited source tree is never modified.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import llm, state
from .analyze import audit_repo
from .config import load_config
from .report import write_report

EXIT_HEALTHY = 0
EXIT_ADVISORY = 1
EXIT_ERROR = 2
EXIT_BLOCKING = 3


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ctxlint",
        description="Test whether agent context files are true against the repo's actual state.",
    )
    p.add_argument("path", nargs="?", default=".", help="Repo path to audit (default: .)")
    p.add_argument("--config", help="Path to a contextlint TOML config")
    p.add_argument("--out", help="Output directory for reports "
                                 "(default: outside the repo, under the state dir)")
    p.add_argument("--state-dir", help="Override the external state directory")
    p.add_argument("--format", default="md,json", help="Comma list: md,json (default: both)")
    p.add_argument("--llm-tasks", help="Write emit-mode task bundles to this JSON path")
    p.add_argument("--llm-results", help="Read agent-produced suggestions from this JSON path")
    p.add_argument("--model", help="Model label to record as provenance")
    p.add_argument("--quiet", action="store_true", help="Suppress the summary line")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"ctxlint: not a directory: {root}", file=sys.stderr)
        return EXIT_ERROR

    try:
        cfg = load_config(args.config)
        if args.state_dir:
            cfg.state_dir = args.state_dir
        formats = [f.strip() for f in args.format.split(",") if f.strip()]

        results = None
        if args.llm_results:
            results = llm.load_results(args.llm_results)

        run = audit_repo(root, cfg, llm_results=results, model=args.model)

        if args.llm_tasks and run.task_bundles:
            llm.write_tasks(args.llm_tasks, run.task_bundles)

        # Default report dir lives OUTSIDE the source tree so `git status` stays clean.
        out_dir = args.out or os.path.join(
            cfg.resolved_state_dir(), state._safe(run.repo_id), "reports")
        written = write_report(run, out_dir, formats)
    except Exception as exc:  # surface loudly, fail with error code
        print(f"ctxlint: error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not args.quiet:
        report_path = written.get("md") or written.get("json") or out_dir
        if run.score is None:
            print(f"ctxlint: 0 file(s), no context files found (score n/a) → {report_path}")
        else:
            counts = run.severity_counts
            gated = sum(1 for fa in run.files if fa.llm_gated and not fa.llm_ran)
            print(
                f"ctxlint: {len(run.files)} file(s), score {run.score}/100, "
                f"findings high {counts['high']}/med {counts['medium']}/low {counts['low']} "
                f"→ {report_path}"
                + (f" ({gated} pending deep analysis)" if gated else "")
            )

    if run.has_blocking:
        return EXIT_BLOCKING
    if not run.files:  # nothing audited — surface in CI without blocking
        return EXIT_ADVISORY
    if run.all_findings:
        return EXIT_ADVISORY
    return EXIT_HEALTHY


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
