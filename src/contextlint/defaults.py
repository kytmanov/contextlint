"""Built-in defaults: discovery globs and the default ruleset.

The default deterministic checks are expressed as rule specs in the SAME shape as
user `[[rules]]`, so the engine is uniform and every check is tunable or disableable
from config. Adding a new *primitive* (in rules.py) is the only thing that needs code.
"""

from __future__ import annotations

# Files agent harnesses auto-load into context. Configurable via [contextlint].globs.
# AGENTS.md is the cross-tool standard; the others are tool-specific. AGENTS.md/CLAUDE.md
# are matched recursively because harnesses load them from subdirectories in monorepos.
DEFAULT_GLOBS = [
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "GEMINI.md",
    ".cursor/rules/**/*.mdc",
    ".github/copilot-instructions.md",
]

# Default token-bloat threshold (approx tokens). Overridable via [gating].token_threshold.
DEFAULT_TOKEN_THRESHOLD = 1200

# Default deterministic rules. Each is a rule spec (TOML-shaped dict).
DEFAULT_RULE_SPECS: list[dict] = [
    {
        "id": "stale-path-reference",
        "description": "Inline-code references to paths that git proves were deleted or renamed.",
        "match": r"`([^`\n]+)`",
        "assert": "path_like_exists",
        "params": {"git_proven_only": True},  # high precision; set false to flag any missing path
        "severity": "high",
        "message": "References a path that git shows was deleted or renamed.",
    },
    {
        "id": "dead-relative-link",
        "description": "Markdown links whose relative target does not resolve.",
        "match": r"\[[^\]]*\]\(([^)]+)\)",
        "assert": "link_resolves",
        "severity": "medium",
        "message": "Markdown link target does not resolve.",
    },
    {
        "id": "token-bloat",
        "description": "Context file exceeds the configured approximate token budget.",
        "assert": "token_threshold",
        "severity": "medium",
        "message": "Context file is large; every agent session pays this token cost.",
    },
]
