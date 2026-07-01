"""Acceptance tests for the generic byproduct-ignore rework (node: gates-byproduct-filter).

Validates:
  - DEFAULT_IGNORE constant (tuple of documented patterns)
  - _read_gitignore(worktree) -> list[str]
  - build_ignore_matcher(worktree, cfg) -> Callable[[str], bool]
  - Removal of _is_ignorable / _IGNORABLE_DIRS / _IGNORABLE_SUFFIXES
  - _CLEAN_ENV no longer forces PYTHONDONTWRITEBYTECODE
  - review.py no longer references _is_ignorable
  - node_gate wires through the new matcher (config + .gitignore patterns)
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.config import Config
from director.models import Node


def _mk_config(gates: dict | None = None) -> Config:
    return Config(
        path=Path("/fake/config.toml"),
        tiers={
            "planner": "c/p",
            "test_author": "c/p",
            "executor": "l/e",
            "explorer": "l/e",
            "reviewer": "c/r",
            "escalation": "c/e",
        },
        gates=gates or {},
        pricing={},
        limits={"flake_runs": 1, "node_timeout_secs": 60, "max_attempts": 2},
        sampling={},
        review={},
    )


# --------------------------------------------------------------------------- #
class DefaultIgnoreConstantTests(unittest.TestCase):
    """DEFAULT_IGNORE is a tuple of exactly the six documented patterns."""

    def test_default_ignore_exists_and_is_tuple(self):
        from director.gates import DEFAULT_IGNORE

        self.assertIsInstance(DEFAULT_IGNORE, tuple)

    def test_default_ignore_contains_pycache(self):
        from director.gates import DEFAULT_IGNORE

        self.assertIn("__pycache__", DEFAULT_IGNORE)

    def test_default_ignore_contains_pytest_cache(self):
        from director.gates import DEFAULT_IGNORE

        self.assertIn(".pytest_cache", DEFAULT_IGNORE)

    def test_default_ignore_contains_mypy_cache(self):
        from director.gates import DEFAULT_IGNORE

        self.assertIn(".mypy_cache", DEFAULT_IGNORE)

    def test_default_ignore_contains_ruff_cache(self):
        from director.gates import DEFAULT_IGNORE

        self.assertIn(".ruff_cache", DEFAULT_IGNORE)

    def test_default_ignore_contains_pyc_glob(self):
        from director.gates import DEFAULT_IGNORE

        self.assertIn("*.pyc", DEFAULT_IGNORE)

    def test_default_ignore_contains_pyo_glob(self):
        from director.gates import DEFAULT_IGNORE

        self.assertIn("*.pyo", DEFAULT_IGNORE)


# --------------------------------------------------------------------------- #
class ReadGitignoreTests(unittest.TestCase):
    """_read_gitignore(worktree) correctly parses (and tolerates missing/unreadable)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rgi-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self):
        from director.gates import _read_gitignore

        return _read_gitignore(self.tmp)

    def test_missing_gitignore_returns_empty_list(self):
        self.assertEqual(self._read(), [])

    def test_oserror_returns_empty_list(self):
        # A directory at .gitignore path causes IsADirectoryError (OSError subclass).
        (self.tmp / ".gitignore").mkdir()
        self.assertEqual(self._read(), [])

    def test_blank_lines_are_skipped(self):
        (self.tmp / ".gitignore").write_text("\n  \n\n")
        self.assertEqual(self._read(), [])

    def test_comment_lines_are_skipped(self):
        (self.tmp / ".gitignore").write_text("# header\n# another comment\n")
        self.assertEqual(self._read(), [])

    def test_negation_lines_are_skipped(self):
        (self.tmp / ".gitignore").write_text("!important.py\n!keep/\n")
        self.assertEqual(self._read(), [])

    def test_plain_pattern_included(self):
        (self.tmp / ".gitignore").write_text("*.log\n")
        self.assertEqual(self._read(), ["*.log"])

    def test_leading_slash_stripped(self):
        (self.tmp / ".gitignore").write_text("/dist\n")
        self.assertEqual(self._read(), ["dist"])

    def test_trailing_slash_stripped(self):
        (self.tmp / ".gitignore").write_text("node_modules/\n")
        self.assertEqual(self._read(), ["node_modules"])

    def test_both_leading_and_trailing_slash_stripped(self):
        (self.tmp / ".gitignore").write_text("/build/\n")
        self.assertEqual(self._read(), ["build"])

    def test_whitespace_stripped_from_patterns(self):
        (self.tmp / ".gitignore").write_text("  *.tmp  \n")
        self.assertEqual(self._read(), ["*.tmp"])

    def test_mixed_lines_in_order(self):
        content = "# header\n\n*.pyc\n!keep.pyc\n/dist/\nnode_modules/\n__pycache__\n"
        (self.tmp / ".gitignore").write_text(content)
        self.assertEqual(self._read(), ["*.pyc", "dist", "node_modules", "__pycache__"])

    def test_empty_pattern_after_stripping_is_skipped(self):
        # A bare "/" becomes "" after stripping — must be skipped.
        (self.tmp / ".gitignore").write_text("/\n//\n*.log\n")
        result = self._read()
        self.assertIn("*.log", result)
        self.assertNotIn("", result)
        self.assertNotIn("/", result)


# --------------------------------------------------------------------------- #
class BuildIgnoreMatcherTests(unittest.TestCase):
    """build_ignore_matcher assembles the full pattern list and returns a correct closure."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bim-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _matcher(self, gates: dict | None = None):
        from director.gates import build_ignore_matcher

        return build_ignore_matcher(self.tmp, _mk_config(gates))

    # --- return type ---------------------------------------------------------

    def test_returns_callable(self):
        import collections.abc

        from director.gates import build_ignore_matcher

        result = build_ignore_matcher(self.tmp, _mk_config())
        self.assertIsInstance(result, collections.abc.Callable)

    # --- DEFAULT_IGNORE patterns are always active ---------------------------

    def test_pyc_basename_matched_by_default(self):
        self.assertTrue(self._matcher()("src/foo.pyc"))

    def test_pyo_basename_matched_by_default(self):
        self.assertTrue(self._matcher()("src/foo.pyo"))

    def test_pycache_segment_matched_by_default(self):
        self.assertTrue(self._matcher()("__pycache__/foo.cpython-311.pyc"))

    def test_pytest_cache_segment_matched_by_default(self):
        self.assertTrue(self._matcher()(".pytest_cache/v/cache/lastfailed"))

    def test_mypy_cache_segment_matched_by_default(self):
        self.assertTrue(self._matcher()(".mypy_cache/3.11/builtins.data"))

    def test_ruff_cache_segment_matched_by_default(self):
        self.assertTrue(self._matcher()(".ruff_cache/content/abc"))

    def test_nested_pyc_matched_by_default(self):
        self.assertTrue(self._matcher()("a/b/c/foo.pyc"))

    def test_real_source_file_not_matched(self):
        self.assertFalse(self._matcher()("director/gates.py"))

    def test_py_extension_not_matched(self):
        self.assertFalse(self._matcher()("src/module.py"))

    # --- segment matching (no / in pattern) ----------------------------------

    def test_dir_pattern_matches_as_segment_at_any_depth(self):
        # "__pycache__" matches if that string is a path segment
        match = self._matcher()
        self.assertTrue(match("a/b/__pycache__/module.cpython-311.pyc"))

    # --- full-path matching (pattern contains /) -----------------------------

    def test_slash_pattern_uses_fnmatch_on_full_path(self):
        (self.tmp / ".gitignore").write_text("dist/*\n")
        match = self._matcher()
        self.assertTrue(match("dist/foo.js"))

    def test_slash_pattern_does_not_match_different_prefix(self):
        (self.tmp / ".gitignore").write_text("dist/*\n")
        match = self._matcher()
        self.assertFalse(match("src/dist/foo.js"))

    # --- config [gates].ignore patterns --------------------------------------

    def test_config_ignore_list_adds_patterns(self):
        match = self._matcher({"ignore": ["*.log"]})
        self.assertTrue(match("app.log"))
        self.assertTrue(match("logs/app.log"))

    def test_config_ignore_list_dir_segment(self):
        match = self._matcher({"ignore": ["target"]})
        self.assertTrue(match("target/debug/build"))

    def test_config_ignore_non_list_treated_as_empty(self):
        match = self._matcher({"ignore": "*.log"})
        self.assertFalse(match("app.log"))

    def test_config_ignore_non_str_entries_excluded(self):
        match = self._matcher({"ignore": [42, "*.tmp", None]})
        self.assertTrue(match("foo.tmp"))
        self.assertFalse(match("42"))

    def test_config_ignore_missing_key_adds_no_patterns(self):
        self.assertFalse(self._matcher({})("app.log"))

    # --- .gitignore-derived patterns -----------------------------------------

    def test_gitignore_pattern_adds_to_matcher(self):
        (self.tmp / ".gitignore").write_text("*.o\n")
        self.assertTrue(self._matcher()("src/main.o"))

    def test_gitignore_dir_pattern_matches_as_segment(self):
        (self.tmp / ".gitignore").write_text("build/\n")  # trailing / stripped → "build"
        self.assertTrue(self._matcher()("build/debug/output"))

    def test_missing_gitignore_gives_no_extra_patterns(self):
        self.assertFalse(self._matcher()("build/debug/output"))

    # --- precedence / combination --------------------------------------------

    def test_all_three_sources_combined(self):
        (self.tmp / ".gitignore").write_text("*.o\n")
        match = self._matcher({"ignore": ["*.log"]})
        self.assertTrue(match("src/foo.pyc"))  # DEFAULT_IGNORE
        self.assertTrue(match("app.log"))  # config
        self.assertTrue(match("src/main.o"))  # .gitignore


# --------------------------------------------------------------------------- #
class RemovedSymbolsTests(unittest.TestCase):
    """Old hardcoded byproduct symbols must be deleted from gates.py."""

    def test_is_ignorable_removed(self):
        import director.gates as g

        self.assertFalse(
            hasattr(g, "_is_ignorable"),
            "_is_ignorable must be deleted from gates.py",
        )

    def test_ignorable_dirs_removed(self):
        import director.gates as g

        self.assertFalse(
            hasattr(g, "_IGNORABLE_DIRS"),
            "_IGNORABLE_DIRS must be deleted from gates.py",
        )

    def test_ignorable_suffixes_removed(self):
        import director.gates as g

        self.assertFalse(
            hasattr(g, "_IGNORABLE_SUFFIXES"),
            "_IGNORABLE_SUFFIXES must be deleted from gates.py",
        )


# --------------------------------------------------------------------------- #
class CleanEnvTests(unittest.TestCase):
    """_CLEAN_ENV must NOT force-set PYTHONDONTWRITEBYTECODE (byproducts are now
    handled by the ignore matcher, not by suppressing bytecode generation)."""

    def test_pythondontwritebytecode_absent_from_clean_env(self):
        import director.gates as g

        self.assertNotIn(
            "PYTHONDONTWRITEBYTECODE",
            g._CLEAN_ENV,
            "_CLEAN_ENV must not contain PYTHONDONTWRITEBYTECODE",
        )


# --------------------------------------------------------------------------- #
class ReviewImportTests(unittest.TestCase):
    """review.py must not reference the deleted _is_ignorable symbol."""

    def test_review_does_not_import_is_ignorable(self):
        review_src = (
            pathlib.Path(__file__).resolve().parent.parent / "director" / "review.py"
        ).read_text()
        self.assertNotIn(
            "_is_ignorable",
            review_src,
            "review.py still references deleted _is_ignorable from gates",
        )


# --------------------------------------------------------------------------- #
class NodeGateByproductFilterTests(unittest.TestCase):
    """node_gate accepts byproduct paths via the new matcher."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ng-bpf-"))
        self._g(["init", "-q"], self.tmp)
        self._g(["config", "user.email", "t@t"], self.tmp)
        self._g(["config", "user.name", "t"], self.tmp)
        self._g(["config", "commit.gpgsign", "false"], self.tmp)
        (self.tmp / "README").write_text("x\n")
        self._g(["add", "-A"], self.tmp)
        self._g(["commit", "-qm", "init"], self.tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _g(self, args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)

    def _node(self, files=("mod.py",)):
        return Node(
            id="n",
            title="n",
            spec="s",
            files=list(files),
            test_cmd='python3 -c "raise SystemExit(0)"',
            tests=[],
            test_hashes={},
        )

    def test_pycache_byproduct_does_not_fail_gate(self):
        """__pycache__ files must not be out-of-scope (repo has no .gitignore)."""
        from director.gates import node_gate

        (self.tmp / "mod.py").write_text("def f(): pass\n")
        pycache = self.tmp / "__pycache__"
        pycache.mkdir()
        (pycache / "mod.cpython-311.pyc").write_bytes(b"\x00" * 16)

        result = node_gate(self._node(), self.tmp, _mk_config())
        self.assertTrue(result.ok, result.detail)

    def test_config_ignore_pattern_honored_in_gate(self):
        """A file matching cfg.gates['ignore'] is treated as a byproduct."""
        from director.gates import node_gate

        (self.tmp / "mod.py").write_text("def f(): pass\n")
        (self.tmp / "app.log").write_bytes(b"logdata")

        result = node_gate(self._node(), self.tmp, _mk_config({"ignore": ["*.log"]}))
        self.assertTrue(result.ok, result.detail)

    def test_out_of_scope_source_still_blocked(self):
        """A real source file not in the allowlist and not ignored must still block."""
        from director.gates import node_gate

        (self.tmp / "mod.py").write_text("def f(): pass\n")
        (self.tmp / "unauthorized.py").write_text("# snuck in\n")

        result = node_gate(self._node(), self.tmp, _mk_config())
        self.assertFalse(result.ok)
        self.assertIn("out-of-scope edits", result.failures)

    def test_config_ignore_non_list_does_not_ignore_extra_files(self):
        """Malformed ignore config (str instead of list) does not match extra files."""
        from director.gates import node_gate

        (self.tmp / "mod.py").write_text("def f(): pass\n")
        (self.tmp / "app.log").write_bytes(b"logdata")

        # "ignore" is a string, not a list → treated as empty → app.log is out-of-scope
        result = node_gate(self._node(), self.tmp, _mk_config({"ignore": "*.log"}))
        self.assertFalse(result.ok)
        self.assertIn("out-of-scope edits", result.failures)


if __name__ == "__main__":
    unittest.main(verbosity=2)
