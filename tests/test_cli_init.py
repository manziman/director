"""Acceptance tests for wiring the `init` subcommand into `director/cli.py`.

Pins the contract for the cli-init-flags node on top of the original cli-wiring
contract:

  * The `init` subparser is registered in `build_parser()` and dispatches to
    `cmd_init`; the module docstring advertises the command.
  * `--repo` still defaults to `.` and is overridable.
  * A mutually-exclusive boolean pair `--user` / `--local` is added: both
    default to ``False``, each lands as a real ``bool`` on the parsed namespace,
    and passing BOTH is rejected by argparse itself (``SystemExit``).
  * `cmd_init` lazily imports from `director.init`, calls
    ``run_init(args.repo, user=args.user, local=args.local)``, prints
    ``Wrote <absolute path>`` using the resolved path, prints ONE additional
    line explaining how the target was chosen (forced via ``--user`` /
    ``--local``, else auto-detected via ``is_inside_git_repo(Path(args.repo))``),
    and returns 0.

Only the `director.init` boundary (``run_init`` / ``is_inside_git_repo``) is
stubbed; the parser and command dispatch are the real code.
"""

import argparse
import io
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import cli  # noqa: E402


def _init_args(repo=".", user=False, local=False):
    """A parsed-namespace stand-in with exactly the attributes cmd_init reads."""
    return argparse.Namespace(repo=repo, user=user, local=local)


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


class InitTargetFlagParsingTests(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_user_and_local_default_to_false_booleans(self):
        args = self.parser.parse_args(["init"])
        self.assertIs(args.user, False)
        self.assertIs(args.local, False)

    def test_user_flag_sets_only_user_true(self):
        args = self.parser.parse_args(["init", "--user"])
        self.assertIs(args.user, True)
        self.assertIs(args.local, False)

    def test_local_flag_sets_only_local_true(self):
        args = self.parser.parse_args(["init", "--local"])
        self.assertIs(args.local, True)
        self.assertIs(args.user, False)

    def test_flags_combine_with_repo(self):
        args = self.parser.parse_args(["init", "--repo", "/x", "--user"])
        self.assertEqual(args.repo, "/x")
        self.assertIs(args.user, True)
        self.assertIs(args.local, False)

    def test_user_and_local_are_mutually_exclusive(self):
        # argparse itself must reject supplying both (mutually-exclusive group).
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["init", "--user", "--local"])


class CmdInitCallTests(unittest.TestCase):
    def test_cmd_init_forwards_repo_and_flags_and_returns_zero(self):
        fake_run_init = mock.Mock(return_value=Path("/target/repo/.director/config.toml"))
        with mock.patch("director.init.run_init", fake_run_init):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_init(_init_args(repo="/target/repo", user=True))
        self.assertEqual(rc, 0)
        fake_run_init.assert_called_once_with("/target/repo", user=True, local=False)

    def test_cmd_init_prints_returned_absolute_path(self):
        returned = Path("/written/.director/config.toml")
        with mock.patch("director.init.run_init", mock.Mock(return_value=returned)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_init(_init_args(repo="/written", local=True))
        out = buf.getvalue()
        self.assertIn("Wrote", out)
        self.assertIn(str(returned.resolve()), out)

    def test_run_init_imported_lazily_not_at_module_top_level(self):
        # The import must live inside cmd_init (mirroring cmd_plan/cmd_run), so
        # the cli module must NOT bind run_init at import time.
        self.assertFalse(
            hasattr(cli, "run_init"),
            "run_init must be imported lazily inside cmd_init, not at module top level",
        )


class CmdInitTargetExplanationTests(unittest.TestCase):
    """cmd_init prints ONE extra line noting how the target was chosen."""

    def _run(self, args):
        returned = Path("/written/.director/config.toml")
        with (
            mock.patch("director.init.run_init", mock.Mock(return_value=returned)),
            mock.patch("director.init.is_inside_git_repo", create=True) as fake_inside,
        ):
            fake_inside.return_value = True
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_init(args)
        return rc, buf.getvalue(), fake_inside

    def test_user_flag_notes_forced_user_via_flag(self):
        rc, out, fake_inside = self._run(_init_args(user=True))
        self.assertEqual(rc, 0)
        self.assertIn("--user", out)
        # A forced choice must not consult git detection.
        fake_inside.assert_not_called()

    def test_local_flag_notes_forced_local_via_flag(self):
        rc, out, fake_inside = self._run(_init_args(local=True))
        self.assertEqual(rc, 0)
        self.assertIn("--local", out)
        fake_inside.assert_not_called()

    def test_auto_detect_consults_is_inside_git_repo_and_mentions_git(self):
        rc, out, fake_inside = self._run(_init_args(repo="/some/repo"))
        self.assertEqual(rc, 0)
        fake_inside.assert_called_once_with(Path("/some/repo"))
        self.assertIn("git", out.lower())


class DocstringTests(unittest.TestCase):
    def test_module_docstring_lists_init(self):
        self.assertIsNotNone(cli.__doc__)
        self.assertIn("init", cli.__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
