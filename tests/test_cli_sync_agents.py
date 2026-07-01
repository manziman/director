"""Acceptance tests for update-sync-agents-callers: cli.py sync-agents output + help text.

Pins three behavioral contracts introduced by the cli.py callers node:
  1. cmd_sync_agents prints the neutral message (not "Synced:") when sync_agents returns [].
  2. cmd_sync_agents prints "Synced:" + indented paths when sync_agents returns non-empty list.
  3. The sync-agents subparser help string is the new neutral conditional text.

Integration tests drive the full path through the real argument parser with a temp repo.
Mock tests isolate cmd_sync_agents dispatch behavior from sync_agents implementation.
"""

from __future__ import annotations

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

from director import cli
from director.config import ROLES

# All six tiers pointing at an OpenCode-owned provider.
_OPENCODE_TOML = (
    "[tiers]\n" + "\n".join(f'{r} = "opencode/anthropic/claude-3-5-sonnet"' for r in ROLES) + "\n"
)

_NEW_HELP_TEXT = (
    "install role agents (writes <repo>/.opencode/ only when an OpenCode provider is configured)"
)
_OLD_HELP_FRAGMENT = "(re)install role agents into <repo>/.opencode"
_NEUTRAL_MSG_FRAGMENT = "No provider-specific agent files needed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_sync_agents_cli(repo_path: str | Path) -> tuple[str, int]:
    """Invoke `director sync-agents --repo <repo>` through the real argument parser."""
    parser = cli.build_parser()
    args = parser.parse_args(["sync-agents", "--repo", str(repo_path)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    return buf.getvalue(), rc


def _cmd_with_mock(written: list[str]) -> tuple[str, int]:
    """Invoke cmd_sync_agents directly with sync_agents mocked to return `written`."""
    ns = type("NS", (), {"repo": "."})()
    with mock.patch("director.cli.sync_agents", return_value=written):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_sync_agents(ns)
    return buf.getvalue(), rc


# ---------------------------------------------------------------------------
# Subparser help string
# ---------------------------------------------------------------------------


class HelpStringTests(unittest.TestCase):
    """The sync-agents subparser help string must be the new neutral conditional text."""

    def setUp(self):
        # Force a wide terminal so argparse does not wrap the (long) help string.
        # argparse's HelpFormatter derives its width from shutil.get_terminal_size,
        # which honors the COLUMNS env var — set it here instead of mutating the CLI.
        self._orig_columns = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = "200"

    def tearDown(self):
        if self._orig_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = self._orig_columns

    def _full_help(self) -> str:
        return cli.build_parser().format_help()

    def test_help_contains_new_neutral_text(self):
        self.assertIn(
            _NEW_HELP_TEXT,
            self._full_help(),
            "Expected new conditional help text in parser help output",
        )

    def test_help_does_not_contain_old_reinstall_text(self):
        self.assertNotIn(
            _OLD_HELP_FRAGMENT,
            self._full_help(),
            "Old '(re)install role agents' text must be replaced",
        )


# ---------------------------------------------------------------------------
# cmd_sync_agents dispatch — mocked sync_agents
# ---------------------------------------------------------------------------


class EmptyListOutputTests(unittest.TestCase):
    """When sync_agents returns [], cmd_sync_agents prints the neutral message, not 'Synced:'."""

    def test_prints_neutral_message(self):
        output, _ = _cmd_with_mock([])
        self.assertIn(
            _NEUTRAL_MSG_FRAGMENT,
            output,
            f"Expected neutral message when sync_agents returns []. Got: {output!r}",
        )

    def test_does_not_print_synced_when_empty(self):
        output, _ = _cmd_with_mock([])
        self.assertNotIn(
            "Synced:", output, f"Must not print 'Synced:' when list is empty. Got: {output!r}"
        )

    def test_returns_zero_when_empty(self):
        _, rc = _cmd_with_mock([])
        self.assertEqual(rc, 0)


class NonEmptyListOutputTests(unittest.TestCase):
    """When sync_agents returns paths, cmd_sync_agents prints 'Synced:' + indented paths."""

    _PATHS = [
        ".opencode/agents/brainstorm.md",
        ".opencode/agents/planner.md",
        ".opencode/opencode.json",
    ]

    def test_prints_synced_header(self):
        output, _ = _cmd_with_mock(self._PATHS)
        self.assertIn(
            "Synced:", output, f"Expected 'Synced:' header when paths are written. Got: {output!r}"
        )

    def test_paths_indented_with_two_spaces(self):
        output, _ = _cmd_with_mock(self._PATHS)
        for path in self._PATHS:
            self.assertIn(
                "  " + path,
                output,
                f"Expected '  {path}' (two-space indent) in output. Got: {output!r}",
            )

    def test_does_not_print_neutral_message(self):
        output, _ = _cmd_with_mock(self._PATHS)
        self.assertNotIn(
            _NEUTRAL_MSG_FRAGMENT, output, "Must not print neutral message when paths are written"
        )

    def test_returns_zero_when_nonempty(self):
        _, rc = _cmd_with_mock(self._PATHS)
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Integration — no .director/config.toml
# ---------------------------------------------------------------------------


class NoconfigIntegrationTests(unittest.TestCase):
    """Integration: no .director/config.toml → neutral message, no .opencode/ created."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-sa-noconfig-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prints_neutral_message(self):
        output, _ = _run_sync_agents_cli(self.tmp)
        self.assertIn(
            _NEUTRAL_MSG_FRAGMENT,
            output,
            f"Expected neutral message for no-config repo. Got: {output!r}",
        )

    def test_no_opencode_directory_created(self):
        _run_sync_agents_cli(self.tmp)
        self.assertFalse(
            (self.tmp / ".opencode").exists(),
            ".opencode/ must not be created when no config.toml is present",
        )

    def test_returns_zero(self):
        _, rc = _run_sync_agents_cli(self.tmp)
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Integration — OpenCode .director/config.toml present
# ---------------------------------------------------------------------------


class OpenCodeConfigIntegrationTests(unittest.TestCase):
    """Integration: OpenCode .director/config.toml → Synced: + paths, .opencode/agents/ exists."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-sa-opencode-"))
        (self.tmp / ".director").mkdir(parents=True)
        (self.tmp / ".director" / "config.toml").write_text(_OPENCODE_TOML)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prints_synced_header(self):
        output, _ = _run_sync_agents_cli(self.tmp)
        self.assertIn(
            "Synced:", output, f"Expected 'Synced:' for OpenCode config repo. Got: {output!r}"
        )

    def test_paths_listed_with_indentation(self):
        output, _ = _run_sync_agents_cli(self.tmp)
        indented = [line for line in output.splitlines() if line.startswith("  ")]
        self.assertTrue(indented, f"Expected indented path lines in output. Got: {output!r}")

    def test_opencode_agents_dir_created(self):
        _run_sync_agents_cli(self.tmp)
        self.assertTrue(
            (self.tmp / ".opencode" / "agents").is_dir(),
            ".opencode/agents/ must be created when OpenCode provider is configured",
        )

    def test_does_not_print_neutral_message(self):
        output, _ = _run_sync_agents_cli(self.tmp)
        self.assertNotIn(
            _NEUTRAL_MSG_FRAGMENT, output, "Must not print neutral message when paths were written"
        )

    def test_returns_zero(self):
        _, rc = _run_sync_agents_cli(self.tmp)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
