"""Intent-driven tests for the ContextLint deterministic core.

Each test encodes *why* a behavior matters, not just that code runs:
- grounding must cite the commit that deleted a referenced path;
- a finding's signature must survive edits (so feedback can suppress it later);
- gating must skip unchanged files and re-fire on change/bloat;
- the audit must never mutate the source tree.

Uses stdlib unittest only (no new dependencies).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextlint.analyze import audit_repo  # noqa: E402
from contextlint.config import Config, load_config  # noqa: E402
from contextlint.llm import verify_suggestions  # noqa: E402
from contextlint.snapshot import build_snapshot  # noqa: E402


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _commit(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                         capture_output=True, text=True)
    return out.stdout.strip()


class TestGrounding(unittest.TestCase):
    def _cfg(self, root):
        cfg = Config()
        cfg.rules = load_config().rules  # default ruleset
        cfg.state_dir = os.path.join(root, "_state")
        return cfg

    def test_stale_path_cites_deleting_commit(self):
        """A reference to a deleted file must be flagged WITH the deleting commit sha."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "foo.py", "print('hi')\n")
            _write(root, "CLAUDE.md", "See `foo.py` for the entrypoint.\n")
            _commit(root, "initial")
            os.remove(os.path.join(root, "foo.py"))
            del_sha = _commit(root, "remove foo")

            result = audit_repo(root, self._cfg(root))
            stale = [f for fa in result.files for f in fa.findings
                     if f.rule_id == "stale-path-reference"]
            self.assertEqual(len(stale), 1, "expected exactly one stale-path finding")
            ev = stale[0].evidence[0]
            self.assertEqual(ev.kind, "path_deleted")
            self.assertEqual(ev.commit, del_sha,
                             "evidence must cite the commit that deleted the path")

    def test_existing_path_not_flagged(self):
        """A reference to a present file must NOT be flagged (no false positive)."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "foo.py", "print('hi')\n")
            _write(root, "CLAUDE.md", "See `foo.py` for the entrypoint.\n")
            _commit(root, "initial")
            result = audit_repo(root, self._cfg(root))
            stale = [f for fa in result.files for f in fa.findings
                     if f.rule_id == "stale-path-reference"]
            self.assertEqual(stale, [])

    def test_never_existed_path_not_flagged_by_default(self):
        """High precision: a path with no git deletion record is NOT a stale reference.

        Docs mention runtime paths, domains, and short names; the default rule only fires
        on git-proven deletion/rename to avoid drowning real issues in noise.
        """
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Outputs go to `build/state.db` at runtime.\n")
            _commit(root, "initial")
            result = audit_repo(root, self._cfg(root))
            stale = [f for fa in result.files for f in fa.findings
                     if f.rule_id == "stale-path-reference"]
            self.assertEqual(stale, [], "never-committed runtime path must not be flagged")

    def test_dead_relative_link(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Read [the guide](./docs/missing.md).\n")
            _commit(root, "initial")
            result = audit_repo(root, self._cfg(root))
            dead = [f for fa in result.files for f in fa.findings
                    if f.rule_id == "dead-relative-link"]
            self.assertEqual(len(dead), 1)

    def test_signature_survives_edit(self):
        """The same issue keeps its signature when surrounding lines shift."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "gone.py", "x = 1\n")
            _write(root, "CLAUDE.md", "Use `gone.py`.\n")
            _commit(root, "v1")
            os.remove(os.path.join(root, "gone.py"))
            _commit(root, "delete gone.py")
            sig1 = audit_repo(root, self._cfg(root)).all_findings[0].signature

            _write(root, "CLAUDE.md", "# Header\n\nIntro paragraph.\n\nUse `gone.py`.\n")
            _commit(root, "v2")
            sig2 = audit_repo(root, self._cfg(root)).all_findings[0].signature
            self.assertEqual(sig1, sig2, "signature must be content-based, not line-based")


class TestGatingAndTrend(unittest.TestCase):
    def _cfg(self, root, threshold=1200):
        cfg = Config()
        cfg.rules = load_config().rules
        cfg.state_dir = os.path.join(root, "_state")
        cfg.token_threshold = threshold
        return cfg

    def test_gating_skips_unchanged_then_refires_on_change(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Stable content.\n")
            _commit(root, "v1")
            cfg = self._cfg(root)

            first = audit_repo(root, cfg)
            self.assertTrue(first.files[0].llm_gated, "first sight should be gated")

            second = audit_repo(root, cfg)
            self.assertFalse(second.files[0].llm_gated,
                             "unchanged file should not re-trigger the LLM pass")

            _write(root, "CLAUDE.md", "Stable content. Now changed.\n")
            third = audit_repo(root, cfg)
            self.assertTrue(third.files[0].llm_gated, "changed file should re-trigger")

    def test_token_trend_and_bloat_flag(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "short\n")
            _commit(root, "v1")
            cfg = self._cfg(root, threshold=20)

            first = audit_repo(root, cfg)
            self.assertIsNone(first.files[0].prev_tokens)

            _write(root, "CLAUDE.md", "word " * 200 + "\n")
            second = audit_repo(root, cfg)
            fa = second.files[0]
            self.assertIsNotNone(fa.token_delta)
            self.assertGreater(fa.token_delta, 0, "growth should show a positive delta")
            self.assertTrue(any(f.rule_id == "token-bloat" for f in fa.findings),
                            "over-threshold file should raise a bloat finding")


class TestSafetyAndVerification(unittest.TestCase):
    def test_audit_does_not_mutate_source(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Use `gone.py`.\n")
            _commit(root, "v1")
            cfg = Config()
            cfg.rules = load_config().rules
            cfg.state_dir = os.path.join(root, "..", "_state_outside")
            cfg.out_dir = os.path.join(tempfile.gettempdir(), "ctxlint_out_unused")
            audit_repo(root, cfg)
            status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                    capture_output=True, text=True).stdout.strip()
            self.assertEqual(status, "", "source tree must stay clean after an audit")

    def test_verifier_drops_unverifiable_llm_suggestion(self):
        """A suggestion citing a path that actually exists is a hallucination -> dropped."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "real.py", "x = 1\n")
            _commit(root, "v1")
            snap = build_snapshot(root)
            raw = [
                {"message": "real.py is gone", "severity": "high",
                 "evidence": [{"kind": "path_deleted", "detail": "claims deleted",
                               "path": "real.py"}]},
                {"message": "ghost.py is gone", "severity": "high",
                 "evidence": [{"kind": "path_missing", "detail": "truly missing",
                               "path": "ghost.py"}]},
            ]
            kept = verify_suggestions("CLAUDE.md", raw, snap)
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].message, "ghost.py is gone")


class TestNoContextFiles(unittest.TestCase):
    """A repo with nothing to audit must not masquerade as a clean 100/100.

    The score is the signal a fleet digest aggregates; a fake 100 for an unaudited
    repo would silently inflate that average. So 'nothing audited' must be a distinct
    state (score None / null), separate from 'audited and clean' (a real 100).
    """

    def _cfg(self, root):
        cfg = Config()
        cfg.rules = load_config().rules
        cfg.state_dir = os.path.join(root, "_state")
        return cfg

    def test_empty_repo_has_no_score(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "main.py", "x = 1\n")  # no context files match the globs
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            self.assertEqual(run.files, [], "no context files should be discovered")
            self.assertIsNone(run.score, "nothing audited -> no score, not a fake 100")
            self.assertIsNone(run.to_dict()["score"], "JSON sidecar must carry null")

    def test_clean_repo_still_scores_100(self):
        """Keying on files-discovered (not findings) must not regress the real 100."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "All good here.\n")  # an auto-loaded context file
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            self.assertEqual(run.all_findings, [], "fixture must produce no findings")
            self.assertEqual(run.score, 100, "audited and clean is a genuine 100")

    def test_human_docs_are_not_audited(self):
        """README/docs are not harness-loaded context, so a doc-only repo audits nothing.

        Auditing files the agent never reads is a category error; such a repo is
        'nothing to audit' (score None), not a fake clean 100.
        """
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "README.md", "Human-facing docs.\n")
            _write(root, "docs/guide.md", "More human docs.\n")
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            self.assertEqual(run.files, [], "human docs must not be discovered as context")
            self.assertIsNone(run.score)

    def test_nested_agent_file_discovered(self):
        """Harnesses load CLAUDE.md/AGENTS.md from subdirectories (monorepos), so we must too."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "pkg/CLAUDE.md", "Subpackage context.\n")
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            self.assertIn("pkg/CLAUDE.md", [fa.file for fa in run.files],
                          "a nested context file must be audited")

    def test_cli_exits_advisory_when_nothing_audited(self):
        from contextlint import cli
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "main.py", "x = 1\n")
            _commit(root, "v1")
            rc = cli.main([root, "--quiet", "--state-dir",
                           os.path.join(root, "_state")])
            self.assertEqual(rc, cli.EXIT_ADVISORY,
                             "missing context files should surface in CI (exit 1)")


class TestCrossReference(unittest.TestCase):
    """@-imports are explicit, harness-loaded references: a missing target is a real
    breakage the agent will hit, so it must be flagged high — but `@` is overloaded
    (emails, decorators), so precision matters as much as recall.
    """

    def _cfg(self, root):
        cfg = Config()
        cfg.rules = load_config().rules
        cfg.state_dir = os.path.join(root, "_state")
        return cfg

    def _xrefs(self, result):
        return [f for fa in result.files for f in fa.findings
                if f.rule_id == "broken-cross-reference"]

    def test_broken_import_flagged_high(self):
        """A @import whose target is absent is a high-severity broken reference."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Load the standards: @docs/MISSING.md\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            xrefs = self._xrefs(result)
            self.assertEqual(len(xrefs), 1)
            self.assertEqual(xrefs[0].severity.value, "high")
            self.assertEqual(xrefs[0].evidence[0].kind, "broken_import")

    def test_valid_import_not_flagged(self):
        """A @import to an existing file must not be flagged (no false positive)."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "docs/STANDARDS.md", "Our standards.\n")
            _write(root, "CLAUDE.md", "Load the standards: @docs/STANDARDS.md\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            self.assertEqual(self._xrefs(result), [])

    def test_decorator_in_code_fence_not_flagged(self):
        """`@` inside a code block is an example, not an import — precision."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md",
                   "Use fixtures:\n\n```python\n@pytest.fixture\ndef db(): ...\n```\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            self.assertEqual(self._xrefs(result), [],
                             "a decorator in a fenced code block is not a broken import")

    def test_email_not_flagged(self):
        """An email address is not a @import."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Questions? ping a@b.com for help.\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            self.assertEqual(self._xrefs(result), [])

    def test_external_home_import_not_flagged(self):
        """A home-dir import (`@~/...`) is outside the repo and can't be validated, so it
        must not masquerade as a broken in-repo reference."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Inherit global rules: @~/.claude/global.md\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            self.assertEqual(self._xrefs(result), [])

    def test_deleted_import_cites_commit(self):
        """A @import to a git-deleted file is grounded with the deleting commit, like a
        stale path reference."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "docs/OLD.md", "old\n")
            _write(root, "CLAUDE.md", "See @docs/OLD.md\n")
            _commit(root, "v1")
            os.remove(os.path.join(root, "docs/OLD.md"))
            del_sha = _commit(root, "drop OLD")
            result = audit_repo(root, self._cfg(root))
            xrefs = self._xrefs(result)
            self.assertEqual(len(xrefs), 1)
            self.assertEqual(xrefs[0].evidence[0].kind, "path_deleted")
            self.assertEqual(xrefs[0].evidence[0].commit, del_sha)

    def test_broken_import_suggests_existing_basename(self):
        """When the target is missing but the same filename exists elsewhere, the finding
        carries a grounded 'did you mean' so the user can fix it, not just see 'broken'."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "STANDARDS.md", "Our standards.\n")  # exists at root, not under docs/
            _write(root, "CLAUDE.md", "Load: @docs/STANDARDS.md\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            xrefs = self._xrefs(result)
            self.assertEqual(len(xrefs), 1)
            self.assertIsNotNone(xrefs[0].replacement)
            self.assertIn("STANDARDS.md", xrefs[0].replacement)


class TestImportGraph(unittest.TestCase):
    """The real context surface is the seed files PLUS everything they @import: those are
    loaded into the agent every session, so they must be audited too — and the walk must
    survive cycles."""

    def _cfg(self, root):
        cfg = Config()
        cfg.rules = load_config().rules
        cfg.state_dir = os.path.join(root, "_state")
        return cfg

    def test_imported_file_is_audited(self):
        """A file pulled in via @import is itself linted, even though no glob matches it."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "notes/EXTRA.md", "Read [the gone doc](./gone.md).\n")
            _write(root, "CLAUDE.md", "Extra context: @notes/EXTRA.md\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            audited = [fa.file for fa in result.files]
            self.assertIn("notes/EXTRA.md", audited,
                          "an @imported file must be audited as loaded context")
            dead = [f for fa in result.files for f in fa.findings
                    if f.rule_id == "dead-relative-link" and f.file == "notes/EXTRA.md"]
            self.assertEqual(len(dead), 1,
                             "findings inside the imported file must surface")

    def test_import_cycle_terminates(self):
        """A→B→A import cycle must not loop forever; each file is audited exactly once."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            # Imports resolve relative to the importing file's directory: from docs/A.md,
            # @B.md is the sibling docs/B.md (and vice-versa).
            _write(root, "docs/A.md", "go to @B.md\n")
            _write(root, "docs/B.md", "back to @A.md\n")
            _write(root, "CLAUDE.md", "start @docs/A.md\n")
            _commit(root, "v1")
            result = audit_repo(root, self._cfg(root))
            audited = [fa.file for fa in result.files]
            self.assertEqual(audited.count("docs/A.md"), 1)
            self.assertEqual(audited.count("docs/B.md"), 1)


class TestScoringV2(unittest.TestCase):
    """The refined score groups findings by category and caps each category's penalty, so
    one noisy dimension can't flatten the headline number — while sub-scores and counts
    keep the detail. The version marker lets downstream consumers detect the formula break.
    """

    def _cfg(self, root):
        cfg = Config()
        cfg.rules = load_config().rules
        cfg.state_dir = os.path.join(root, "_state")
        return cfg

    def test_clean_repo_scores_100(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "All good here.\n")
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            self.assertEqual(run.all_findings, [])
            self.assertEqual(run.score, 100)

    def test_nothing_audited_has_no_category_scores(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "main.py", "x = 1\n")
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            self.assertIsNone(run.score)
            self.assertIsNone(run.category_scores,
                              "no audit -> no category scores, not a fake set of 100s")

    def test_category_penalty_is_capped(self):
        """Four high reference findings (raw penalty 60) are capped at 40, so the score is
        60 not 40 — the cap is what keeps one dimension from dominating."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md",
                   "@a/x.md and @b/y.md and @c/z.md and @d/w.md\n")
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            xrefs = [f for fa in run.files for f in fa.findings
                     if f.rule_id == "broken-cross-reference"]
            self.assertEqual(len(xrefs), 4, "fixture must produce four broken imports")
            self.assertEqual(run.score, 60, "4*15=60 raw, capped at 40 -> 100-40=60")
            self.assertEqual(run.category_scores["reference"], 60)

    def test_sidecar_marks_score_version_and_categories(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            _write(root, "CLAUDE.md", "Load @docs/MISSING.md\n")
            _commit(root, "v1")
            run = audit_repo(root, self._cfg(root))
            payload = run.to_dict()
            self.assertEqual(payload["score_version"], 2)
            self.assertIsInstance(payload["category_scores"], dict)
            self.assertIn("reference", payload["category_scores"])


if __name__ == "__main__":
    unittest.main()
