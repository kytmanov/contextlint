"""Configuration loading and rule assembly.

M1 resolution: built-in defaults, optionally overlaid by a repo-local TOML config
(`--config`). Org-baseline inheritance and `locked` rules arrive in a later milestone;
the layering here is written so that adding a baseline layer is additive.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from . import defaults
from .models import Enforcement, Rule, Severity


@dataclass
class Config:
    globs: list[str] = field(default_factory=lambda: list(defaults.DEFAULT_GLOBS))
    out_dir: str = ".contextlint-out"
    state_dir: str = "~/.context-audit"
    token_threshold: int = defaults.DEFAULT_TOKEN_THRESHOLD
    rules: list[Rule] = field(default_factory=list)
    llm_rules: list[dict] = field(default_factory=list)
    provider: str = "agent"  # agent (emit) | openai (later) | bedrock-native (later)

    def resolved_state_dir(self) -> str:
        return os.path.abspath(os.path.expanduser(self.state_dir))


def _rule_from_spec(spec: dict) -> Rule:
    return Rule(
        id=spec["id"],
        description=spec.get("description", ""),
        match=spec.get("match"),
        assertion=spec.get("assert", "present"),
        params=spec.get("params", {}),
        severity=Severity(spec.get("severity", "medium")),
        enforcement=Enforcement(spec.get("enforcement", "advisory")),
        message=spec.get("message", ""),
        scope=spec.get("scope", []),
        ignore=spec.get("ignore", []),
        enabled=spec.get("enabled", True),
        locked=spec.get("locked", False),
    )


def _apply_checks(rules: list[Rule], checks: dict) -> None:
    """Apply [checks.<id>] knob overrides in place (enabled/severity/enforcement)."""
    by_id = {r.id: r for r in rules}
    for rule_id, knobs in checks.items():
        rule = by_id.get(rule_id)
        if rule is None:
            continue
        if "enabled" in knobs:
            rule.enabled = bool(knobs["enabled"])
        if "severity" in knobs:
            rule.severity = Severity(knobs["severity"])
        if "enforcement" in knobs:
            rule.enforcement = Enforcement(knobs["enforcement"])
        if "message" in knobs:
            rule.message = knobs["message"]


def load_config(config_path: str | None = None) -> Config:
    """Build the effective config: defaults overlaid by an optional repo-local TOML."""
    cfg = Config()
    cfg.rules = [_rule_from_spec(s) for s in defaults.DEFAULT_RULE_SPECS]

    data: dict = {}
    if config_path:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)

    section = data.get("contextlint", {})
    if "globs" in section:
        cfg.globs = list(section["globs"])
    if "out_dir" in section:
        cfg.out_dir = section["out_dir"]
    if "state_dir" in section:
        cfg.state_dir = section["state_dir"]
    if "provider" in section:
        cfg.provider = section["provider"]

    gating = data.get("gating", {})
    if "token_threshold" in gating:
        cfg.token_threshold = int(gating["token_threshold"])

    # User-defined declarative rules extend the defaults.
    for spec in data.get("rules", []):
        cfg.rules.append(_rule_from_spec(spec))

    cfg.llm_rules = list(data.get("llm_rules", []))

    _apply_checks(cfg.rules, data.get("checks", {}))
    return cfg
