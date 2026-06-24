"""Acceptance tests for wiring the `init` subcommand into `director/cli.py`.

These pin the contract for the cli-wiring node: the `init` subparser is
registered in `build_parser()` with `--repo` defaulting to `.` and dispatching
to `cmd_init`; the module docstring advertises the command; and `cmd_init`
lazily imports `director.init.run_init`, calls it with `args.repo`, prints a
confirmation containing the returned path, and returns 0.

Only the `run_init` boundary is stubbed; the parser and command dispatch are the
real code.
"""

import io
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import cli


class InitSubparserRegistrationTests(unittest.TestCase):
    def test_init_subparser_is_registered_and_dispatches_to_cmd_init(self):
        parser = cli.build_parser()
        args = parser.parse_args(["init"])
        self.assertEqual(args.func, cli.cmd_init)

    def test_init_repo_defaults_to_dot(self):
        parser = cli.build_parser()
        args = parser.parse_args(["init"])
        self.assertEqual(args.repo, ".")

    def test_init_repo_is_overridable(self):
        parser = cli.build_parser()
        args = parser.parse_args(["init", "--repo", "/some/where"])
        self.assertEqual(args.repo, "/some/where")


class CmdInitTests(unittest.TestCase):
    def test_cmd_init_calls_run_init_with_repo_and_returns_zero(self):
        ns = mock.Mock()
        ns.repo = "/target/repo"
        fake_run_init = mock.Mock(return_value="/target/repo/.director/config.toml")
        with mock.patch("director.init.run_init", fake_run_init):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_init(ns)
        self.assertEqual(rc, 0)
        fake_run_init.assert_called_once_with("/target/repo")

    def test_cmd_init_prints_returned_path(self):
        ns = mock.Mock()
        ns.repo = "."
        returned = "/written/.director/config.toml"
        with mock.patch("director.init.run_init", mock.Mock(return_value=returned)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_init(ns)
        self.assertIn(returned, buf.getvalue())

    def test_run_init_imported_lazily_not_at_module_top_level(self):
        # The import must live inside cmd_init (mirroring cmd_plan/cmd_run), so
        # the cli module must NOT bind run_init at import time.
        self.assertFalse(
            hasattr(cli, "run_init"),
            "run_init must be imported lazily inside cmd_init, not at module top level",
        )


class DocstringTests(unittest.TestCase):
    def test_module_docstring_lists_init(self):
        self.assertIsNotNone(cli.__doc__)
        self.assertIn("init", cli.__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
