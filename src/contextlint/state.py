"""External, per-repo run state.

Lives outside the audited repo (default `~/.context-audit/<repo-id>/`) so the source
tree is never touched and gating/trend survive between runs. In a later milestone the
same store holds shared feedback; for M1 it records per-file token counts and content
hashes (for trend + gating) plus the audited HEAD.
"""

from __future__ import annotations

import hashlib
import json
import os


def _safe(repo_id: str) -> str:
    return repo_id.replace("/", "__").replace(":", "_")


def _repo_dir(state_dir: str, repo_id: str) -> str:
    return os.path.join(state_dir, _safe(repo_id))


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def load_last_run(state_dir: str, repo_id: str) -> dict:
    """Return the previous run record, or {} if there is none."""
    path = os.path.join(_repo_dir(state_dir, repo_id), "last_run.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_run(state_dir: str, repo_id: str, record: dict) -> str:
    """Persist this run's record; returns the path written."""
    repo_dir = _repo_dir(state_dir, repo_id)
    os.makedirs(repo_dir, exist_ok=True)
    path = os.path.join(repo_dir, "last_run.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    return path
