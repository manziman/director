"""Acceptance tests for wiring the `ui` subcommand into `director/cli.py`.

Mirrors the cli-wiring contract pinned by test_cli_init.py:

  * The `ui` subparser is registered in `build_parser()` and dispatches to
    `cmd_ui`; the module docstring advertises the command.
  * Flags: `--repo` defaults to `.`, `--host` to `127.0.0.1`, `--port` to
    8642 (int), `--open` to False — all overridable.
  * `cmd_ui` lazily imports `serve` from `director.web.server` (not at module
    top level), calls it with the parsed flags, and returns 0.

Only the `director.web.server.serve` boundary is stubbed; the parser and
command dispatch are the real code.
"""

import argparse
import os
import pathlib
import sys
import unittest
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import cli  # noqa: E402


class UiSubparserRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_ui_subparser_dispatches_to_cmd_ui(self):
        args = self.parser.parse_args(["ui"])
        self.assertEqual(args.func, cli.cmd_ui)

    def test_defaults(self):
        args = self.parser.parse_args(["ui"])
        self.assertEqual(args.repo, ".")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8642)
        self.assertIs(args.open, False)

    def test_flags_overridable(self):
        args = self.parser.parse_args(
            ["ui", "--repo", "/x", "--host", "0.0.0.0", "--port", "0", "--open"]
        )
        self.assertEqual(args.repo, "/x")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 0)
        self.assertIs(args.open, True)

    def test_port_is_int(self):
        args = self.parser.parse_args(["ui", "--port", "9999"])
        self.assertIsInstance(args.port, int)


class CmdUiCallTests(unittest.TestCase):
    def test_cmd_ui_forwards_flags_and_returns_zero(self):
        ns = argparse.Namespace(repo="/target", host="127.0.0.1", port=1234, open=True)
        with mock.patch("director.web.server.serve") as fake_serve:
            rc = cli.cmd_ui(ns)
        self.assertEqual(rc, 0)
        fake_serve.assert_called_once_with(
            "/target", host="127.0.0.1", port=1234, open_browser=True, log=cli._log
        )

    def test_serve_imported_lazily_not_at_module_top_level(self):
        self.assertFalse(
            hasattr(cli, "serve"),
            "serve must be imported lazily inside cmd_ui, not at module top level",
        )


class DocstringTests(unittest.TestCase):
    def test_module_docstring_lists_ui(self):
        self.assertIsNotNone(cli.__doc__)
        self.assertIn("ui", cli.__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
