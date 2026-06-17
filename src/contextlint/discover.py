"""Discover context files by applying the configured globs."""

from __future__ import annotations

from pathlib import Path


def discover(root: str, globs: list[str], out_dir: str = ".contextlint-out") -> list[str]:
    """Return repo-relative paths of existing files matching any glob.

    The output directory is excluded so prior reports are never re-audited.
    """
    root_p = Path(root)
    out_prefix = out_dir.strip("/") + "/"
    found: set[str] = set()
    for pattern in globs:
        for path in root_p.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root_p).as_posix()
            if rel == out_dir or rel.startswith(out_prefix):
                continue
            found.add(rel)
    return sorted(found)
