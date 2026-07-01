"""Acceptance tests for the `init-target-selection` node.

`director/init.py` must gain hermetic (no-subprocess, no-network, no git
binary) git detection and init-target resolution, and `run_init` must be
rewired to write to the resolved target:

  * ``is_inside_git_repo(start)`` walks up from ``start.resolve()`` and returns
    True as soon as a ``.git`` *directory or file* exists at that level (the
    file form covers worktrees/submodules); False if the walk reaches the
    filesystem root with no match.
  * ``resolve_init_target(repo, *, user, local)`` returns the config path:
      - ``user``  -> ``_user_config_path()``   (user wins over local)
      - ``local`` -> ``<repo>/.director/config.toml``
      - else      -> repo path if inside a git repo, else user path
  * ``run_init(repo, *, user=False, local=False)`` computes the target via
    ``resolve_init_target``, runs the SAME overwrite-confirmation prompt but
    guarding the selected target, then discovers/prompts/renders/writes to it
    and returns it.  The positional call ``run_init(repo)`` still works.

These tests never invoke real git: a repo is faked by creating a ``.git``
directory (or file) in a temp tree.  HOME is monkeypatched to an isolated temp
dir so the "user" target never touches the real user config.
"""

import builtins
import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import config, init  # noqa: E402
from director.config import ROLES  # noqa: E402


def scripted_input(answers):
    """Return an input() replacement yielding `answers`, recording prompts.

    Raises AssertionError if asked for more answers than scripted so an
    accidental re-prompt loop fails loudly instead of hanging.
    """
    it = iter(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        try:
            return next(it)
        except StopIteration as exc:  # pragma: no cover - defensive
            raise AssertionError(
                f"input() asked for more than the {len(answers)} scripted answers; "
                f"prompts so far: {prompts!r}"
            ) from exc

    fake_input.prompts = prompts
    return fake_input


def _menu_answers(gate_answers):
    """One numeric selection per role (always model #1) then the three gates."""
    return ["1"] * len(ROLES) + list(gate_answers)


# --------------------------------------------------------------------------- #
class IsInsideGitRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-git-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_clean_temp_tree_returns_false(self):
        self.assertFalse(init.is_inside_git_repo(self.tmp))

    def test_clean_nested_dir_returns_false(self):
        nested = self.tmp / "a" / "b" / "c"
        nested.mkdir(parents=True)
        self.assertFalse(init.is_inside_git_repo(nested))

    def test_git_dir_at_start_returns_true(self):
        (self.tmp / ".git").mkdir()
        self.assertTrue(init.is_inside_git_repo(self.tmp))

    def test_git_dir_in_ancestor_returns_true(self):
        (self.tmp / ".git").mkdir()
        nested = self.tmp / "src" / "pkg"
        nested.mkdir(parents=True)
        self.assertTrue(init.is_inside_git_repo(nested))

    def test_git_file_counts_as_repo(self):
        # A `.git` *file* (worktree / submodule) must be recognised too.
        (self.tmp / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
        self.assertTrue(init.is_inside_git_repo(self.tmp))

    def test_git_file_in_ancestor_returns_true(self):
        (self.tmp / ".git").write_text("gitdir: /elsewhere\n")
        nested = self.tmp / "deep" / "dir"
        nested.mkdir(parents=True)
        self.assertTrue(init.is_inside_git_repo(nested))

    def test_returns_bool(self):
        self.assertIsInstance(init.is_inside_git_repo(self.tmp), bool)


# --------------------------------------------------------------------------- #
class _HomeIsolatedTestCase(unittest.TestCase):
    """Base case that isolates HOME/USERPROFILE to a fresh temp dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-target-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = Path(tempfile.mkdtemp(prefix="fm-target-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self._saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    @property
    def user_path(self):
        # Contract of `_user_config_path()` (config-two-level-merge node).
        return Path.home() / ".director" / "config.toml"

    @property
    def repo_path(self):
        return Path(str(self.tmp)) / ".director" / "config.toml"

    def _make_git_repo(self):
        (self.tmp / ".git").mkdir()


# --------------------------------------------------------------------------- #
class ResolveInitTargetTests(_HomeIsolatedTestCase):
    def test_local_forces_repo_path(self):
        # No .git present; --local must still pick the repo path.
        target = init.resolve_init_target(str(self.tmp), user=False, local=True)
        self.assertEqual(target, self.repo_path)

    def test_user_forces_user_path(self):
        # Even inside a git repo, --user must pick the user path.
        self._make_git_repo()
        target = init.resolve_init_target(str(self.tmp), user=True, local=False)
        self.assertEqual(target, self.user_path)

    def test_user_beats_local_when_both_set(self):
        target = init.resolve_init_target(str(self.tmp), user=True, local=True)
        self.assertEqual(target, self.user_path)

    def test_auto_detect_inside_git_repo_picks_repo(self):
        self._make_git_repo()
        target = init.resolve_init_target(str(self.tmp), user=False, local=False)
        self.assertEqual(target, self.repo_path)

    def test_auto_detect_outside_git_repo_picks_user(self):
        # Clean temp tree (no .git): auto-detect falls back to the user path.
        target = init.resolve_init_target(str(self.tmp), user=False, local=False)
        self.assertEqual(target, self.user_path)


# --------------------------------------------------------------------------- #
class RunInitTargetTests(_HomeIsolatedTestCase):
    def _run(self, *, models=None, answers, **kwargs):
        if models is None:
            models = ["opencode/anthropic/claude-opus-4", "opencode/openai/gpt-4o"]
        fi = scripted_input(answers)
        buf = io.StringIO()
        with (
            mock.patch.object(init, "discover_models", return_value=models),
            mock.patch.object(builtins, "input", fi),
            redirect_stdout(buf),
        ):
            path = init.run_init(str(self.tmp), **kwargs)
        return path, buf.getvalue(), fi

    # -- target selection round-trips through the writer ----------------------

    def test_local_writes_repo_path(self):
        path, _, _ = self._run(answers=_menu_answers(["", "", ""]), local=True)
        self.assertEqual(path, self.repo_path)
        self.assertTrue(self.repo_path.exists())
        cfg = config.load_file(self.repo_path)
        self.assertEqual(set(cfg.tiers), set(ROLES))

    def test_user_writes_user_path(self):
        path, _, _ = self._run(answers=_menu_answers(["", "", ""]), user=True)
        self.assertEqual(path, self.user_path)
        self.assertTrue(self.user_path.exists())
        # Nothing written under the repo.
        self.assertFalse(self.repo_path.exists())
        cfg = config.load_file(self.user_path)
        self.assertEqual(set(cfg.tiers), set(ROLES))

    def test_auto_detect_writes_repo_path_when_git_present(self):
        self._make_git_repo()
        path, _, _ = self._run(answers=_menu_answers(["", "", ""]))
        self.assertEqual(path, self.repo_path)
        self.assertTrue(self.repo_path.exists())
        self.assertFalse(self.user_path.exists())

    def test_auto_detect_writes_user_path_when_no_git(self):
        path, _, _ = self._run(answers=_menu_answers(["", "", ""]))
        self.assertEqual(path, self.user_path)
        self.assertTrue(self.user_path.exists())
        self.assertFalse(self.repo_path.exists())

    def test_positional_call_still_works(self):
        # run_init(repo) with no keywords must behave like auto-detect.
        self._make_git_repo()
        path, _, _ = self._run(answers=_menu_answers(["", "", ""]))
        self.assertEqual(path, self.repo_path)

    # -- overwrite prompt guards the SELECTED target --------------------------

    def test_overwrite_prompt_fires_against_selected_user_target(self):
        # Pre-existing USER config; --user must trigger the overwrite prompt and
        # a declined answer must leave it untouched (only one prompt asked).
        self.user_path.parent.mkdir(parents=True)
        self.user_path.write_text("ORIGINAL\n")
        path, _, fi = self._run(answers=["n"], user=True)
        self.assertEqual(path, self.user_path)
        self.assertEqual(self.user_path.read_text(), "ORIGINAL\n")
        self.assertEqual(len(fi.prompts), 1)

    def test_overwrite_prompt_ignores_nonselected_repo_target(self):
        # A stale repo config must NOT trigger the overwrite prompt when the
        # selected target is the user path (guard is against `target`, not the
        # hardcoded repo path).  The user target is written, repo file untouched.
        self.repo_path.parent.mkdir(parents=True)
        self.repo_path.write_text("ORIGINAL\n")
        path, _, _ = self._run(answers=_menu_answers(["", "", ""]), user=True)
        self.assertEqual(path, self.user_path)
        self.assertTrue(self.user_path.exists())
        self.assertEqual(self.repo_path.read_text(), "ORIGINAL\n")

    def test_overwrite_accepted_rewrites_selected_target(self):
        self.repo_path.parent.mkdir(parents=True)
        self.repo_path.write_text("ORIGINAL\n")
        self._run(answers=["y"] + _menu_answers(["", "", ""]), local=True)
        self.assertNotEqual(self.repo_path.read_text(), "ORIGINAL\n")
        cfg = config.load_file(self.repo_path)
        self.assertEqual(set(cfg.tiers), set(ROLES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
