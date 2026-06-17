# ContextLint

ContextLint lints the instruction files your agent harness auto-loads — `AGENTS.md`,
`CLAUDE.md`, `GEMINI.md`, `.cursor/rules/**`, `.github/copilot-instructions.md` — against the
repo's actual state. It flags stale path references, dead links, and token bloat, with concrete
evidence for every finding. It produces grounded suggestions and never modifies your source.

```
ctxlint .                    # audit the current repo
ctxlint /path/to/repo        # audit another repo
```

## Why

Agent instruction files drift. A path gets deleted, a doc keeps pointing at it; a file is
renamed, a link goes dead; the file grows until every session pays a token tax for guidance
that no longer matches the code. The agent then acts on stale context.

ContextLint catches that drift. Every finding cites a concrete repo fact, e.g. *"references
`src/legacy.py` but git shows it was deleted in commit 4d60db1b."* Reports are written
**outside** the repo (under the state dir), so `git status` stays clean and the audited tree is
never touched.

## Install

Python 3.11+ (uses stdlib `tomllib`), no third-party dependencies. The environment is managed
with [uv](https://docs.astral.sh/uv/).

```
uv sync                              # create the env (editable install)
uv run ctxlint .                     # run the CLI
```

## Usage

```
ctxlint [path] [--config FILE] [--out DIR] [--state-dir DIR]
        [--format md,json] [--llm-tasks FILE] [--llm-results FILE] [--model LABEL]
```

Exit codes are CI-friendly:

| Code | Meaning |
|------|---------|
| `0`  | healthy — no findings |
| `1`  | advisory findings present |
| `2`  | error |
| `3`  | blocking policy violation |

### No-LLM (emit) workflow

ContextLint runs without any external LLM. For files that warrant deeper analysis, it writes
self-contained task bundles; a host Claude session fills them in, and the verified suggestions
are merged back into the report.

```
ctxlint . --llm-tasks tasks.json      # 1. write grounded task bundles
# 2. a host Claude session fills tasks.json -> results.json (grounded suggestions)
ctxlint . --llm-results results.json  # 3. merge verified suggestions into the report
```

Merged suggestions are verified against the repo snapshot — any claim that doesn't check out
(e.g. "path X is missing" when X exists) is dropped. LLM findings are always advisory.

## How it works

One run is a fixed pipeline:

```
snapshot ─▶ discover ─▶ evaluate ─▶ gate ─▶ score ─▶ report
```

1. **Snapshot** — take one factual picture of the repo: files and directories that exist now,
   parsed manifests, and git history for deleted/renamed paths. Every finding must trace back to
   a fact here. Git access is best-effort; a non-git tree just yields less evidence.
2. **Discover** — apply the configured globs to find the context files to audit (the output
   directory is excluded so prior reports are never re-audited).
3. **Evaluate** — run the deterministic rule engine over each file. A rule is a `match` regex
   plus an `assert` primitive (does this referenced path exist? does this link resolve? is the
   file over budget?). Each check returns concrete evidence or nothing.
4. **Gate** — decide whether the expensive LLM pass is worth it. It runs only for files that are
   new, changed since the last run, or over the token budget. Per-file token counts and content
   hashes are kept in the external state dir to make this work across runs.
5. **Score** — compute a deterministic, model-independent health score (100 = clean; each
   finding subtracts a severity-weighted penalty).
6. **Report** — render a dated Markdown report plus a machine-readable JSON sidecar, written
   outside the source tree.

## Configuration

Point `--config` at a TOML file. Built-in checks ship as default rules, so they are uniform with
your own and fully tunable. There are three ways to customize validation without writing code —
all shown in `examples/contextlint.toml`:

- **Knobs** on built-in checks — change severity/enforcement or disable a check.
- **Declarative rules** — new deterministic checks built from existing primitives.
- **LLM rules** — natural-language semantic checks evaluated in the gated pass.

Adding a new *primitive* is the only change that needs code; new *rules* are pure config.

## Status & roadmap

Milestone 1 (deterministic core) is implemented:

- Discovery of context files via configurable globs.
- A grounding snapshot (tree, manifests, git deletions/renames, stable repo id).
- A declarative rule engine with built-in checks: stale path references (with deleting commit),
  dead relative links, and token bloat with run-to-run trend.
- Approximate token-cost estimation per file.
- A gated LLM pass that only runs for new, changed, or over-budget files.
- Emit mode: no external LLM required — ContextLint writes task bundles a host Claude session
  fills in, then merges the verified suggestions.
- Dated Markdown report + JSON sidecar (health score, severity counts, provider/model
  provenance).

Later milestones add fleet aggregation, org-baseline governance with `advisory|blocking`
enforcement, shared team feedback, multi-tool divergence detection, and an OpenAI-compatible
HTTP provider (local LM Studio / AWS Bedrock).

## Development

```
uv sync                              # create the env (editable install)
uv run ctxlint .                     # run the CLI
uv run -m unittest discover tests    # run the tests
```

## License

MIT — see [LICENSE](LICENSE).
