"""Build the grounding snapshot: one factual picture of the repo per run.

Every suggestion must cite something from here. Git access is via subprocess to the
system `git` and tolerates non-git trees and shallow history (it just yields less
evidence). Nothing in this module writes to the repo.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib

from .models import Snapshot

_SHA_LINE = re.compile(r"^[0-9a-f]{40}$")
_PRUNE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
               ".pytest_cache", ".mypy_cache", ".eggs"}


def _git(root: str, *args: str) -> str | None:
    """Run a git command in `root`, returning stdout or None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _repo_id(root: str) -> str:
    """Stable id from the origin remote, normalized; falls back to the dir name.

    Normalizing both SSH and HTTPS remote forms to `host/owner/repo` means the same
    repo keys to the same state regardless of how it was cloned or where it sits on disk.
    """
    remote = _git(root, "remote", "get-url", "origin")
    if remote:
        url = remote.strip()
        url = re.sub(r"^\w+://", "", url)  # strip scheme
        url = re.sub(r"^[^@]+@", "", url)  # strip user@
        url = url.replace(":", "/", 1)  # scp-style host:owner -> host/owner
        url = re.sub(r"\.git$", "", url)
        return url.lower()
    return os.path.basename(os.path.abspath(root)).lower()


def _walk_tree(root: str) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            dirs.add(rel_dir.replace(os.sep, "/"))
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            files.add(rel.replace(os.sep, "/"))
    return files, dirs


def _manifests(root: str, files: set[str]) -> dict[str, dict]:
    """Best-effort parse of the key manifests present in the tree."""
    out: dict[str, dict] = {}
    if "pyproject.toml" in files:
        try:
            with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
                out["pyproject.toml"] = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    if "package.json" in files:
        try:
            with open(os.path.join(root, "package.json"), encoding="utf-8") as fh:
                out["package.json"] = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    return out


def _deleted_paths(root: str) -> dict[str, str]:
    """Map repo path -> sha of the most recent commit that deleted it."""
    out: dict[str, str] = {}
    log = _git(root, "log", "--diff-filter=D", "--name-only", "--pretty=format:%H")
    if not log:
        return out
    current = None
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        if _SHA_LINE.match(line):
            current = line
        elif current and line not in out:  # newest-first: keep the first (latest) deletion
            out[line.replace(os.sep, "/")] = current
    return out


def _renamed_paths(root: str) -> dict[str, str]:
    """Map old path -> new path for renames in history."""
    out: dict[str, str] = {}
    log = _git(root, "log", "--diff-filter=R", "-M", "--name-status", "--pretty=format:")
    if not log:
        return out
    for line in log.splitlines():
        if not line.startswith("R"):
            continue
        parts = line.split("\t")
        if len(parts) == 3:
            _, old, new = parts
            old = old.replace(os.sep, "/")
            if old not in out:
                out[old] = new.replace(os.sep, "/")
    return out


def build_snapshot(root: str) -> Snapshot:
    root = os.path.abspath(root)
    files, dirs = _walk_tree(root)
    head = _git(root, "rev-parse", "HEAD")
    return Snapshot(
        root=root,
        repo_id=_repo_id(root),
        files=files,
        dirs=dirs,
        basenames={f.rsplit("/", 1)[-1] for f in files},
        manifests=_manifests(root, files),
        deleted_paths=_deleted_paths(root),
        renamed_paths=_renamed_paths(root),
        head=head.strip() if head else None,
    )
