"""Acceptance tests for director.proc (shared subprocess execution helpers).

These tests pin the public surface of the NEW stdlib-only module `director.proc`:
ShellOutcome, _shell_argv, run_shell, _popen_group_kwargs, popen_tree, kill_tree.

No real long-running work, network, or randomness is used. Platform branches are
exercised deterministically by patching `director.proc.os.name` (and, for the
Windows-only flags, by injecting the platform attributes that POSIX lacks).
`subprocess.run`/`subprocess.Popen`/`subprocess.run`-for-taskkill are monkeypatched
on the module's own `subprocess` reference so no real processes are spawned in the
unit-level tests. A couple of POSIX integration tests drive a real `/bin/sh`.

Run: python -m unittest tests.test_proc -v
"""

import ast
import dataclasses
import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import proc, provider  # noqa: E402

_PROC_SRC = pathlib.Path(__file__).resolve().parent.parent / "director" / "proc.py"
_IS_POSIX = os.name != "nt"


def setUpModule():
    """Refresh proc after provider registry isolation tests reload director.provider."""
    import director.codex as codex

    importlib.reload(provider)
    importlib.reload(proc)
    importlib.reload(codex)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeCompleted:
    """Stand-in for the object returned by subprocess.run."""

    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


class _RecordingRun:
    """Captures the args/kwargs of a single subprocess.run call."""

    def __init__(self, result=None, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


class _RecordingPopen:
    """Captures the args/kwargs of a single subprocess.Popen call."""

    def __init__(self):
        self.calls = []
        self.instance = object()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.instance


class _RecordingProc:
    """A fake Popen whose pid does not map to a real process group."""

    def __init__(self, pid=999_999_999):
        self.pid = pid
        self.waited = 0

    def wait(self, *args, **kwargs):
        self.waited += 1
        return -9


class _FakeHandle:
    """A fake popen_tree handle: communicate() returns (stdout, None) or raises
    TimeoutExpired on its first call (to exercise the timeout branch)."""

    def __init__(self, returncode=0, stdout="", raise_timeout=False):
        self.returncode = returncode
        self._stdout = stdout
        self._raise_timeout = raise_timeout
        self.pid = 4242
        self.communicate_calls = []

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if self._raise_timeout and len(self.communicate_calls) == 1:
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        return (self._stdout, None)


class _RecordingPopenTree:
    """Captures the args/kwargs of a single popen_tree call; returns a fixed handle."""

    def __init__(self, handle):
        self.handle = handle
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.handle


# --------------------------------------------------------------------------- #
# ShellOutcome
# --------------------------------------------------------------------------- #
class TestShellOutcome(unittest.TestCase):
    def test_is_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(proc.ShellOutcome))

    def test_field_names_and_order(self):
        names = [f.name for f in dataclasses.fields(proc.ShellOutcome)]
        self.assertEqual(names, ["returncode", "output", "timed_out"])

    def test_positional_construction(self):
        outcome = proc.ShellOutcome(7, "merged text", True)
        self.assertEqual(outcome.returncode, 7)
        self.assertEqual(outcome.output, "merged text")
        self.assertTrue(outcome.timed_out)


# --------------------------------------------------------------------------- #
# _shell_argv
# --------------------------------------------------------------------------- #
class TestShellArgvPosix(unittest.TestCase):
    def test_posix_uses_bin_sh(self):
        with mock.patch.object(proc.os, "name", "posix"):
            self.assertEqual(proc._shell_argv("echo hi"), ["/bin/sh", "-c", "echo hi"])

    def test_posix_ignores_which(self):
        # On POSIX the result is fixed regardless of what shutil.which would return.
        with (
            mock.patch.object(proc.os, "name", "posix"),
            mock.patch.object(proc.shutil, "which", lambda _n: "/somewhere/bash"),
        ):
            self.assertEqual(proc._shell_argv("ls"), ["/bin/sh", "-c", "ls"])


class TestShellArgvWindows(unittest.TestCase):
    def test_windows_prefers_sh(self):
        def which(name):
            return {"sh": "C:\\tools\\sh.exe", "bash": "C:\\tools\\bash.exe"}.get(name)

        with (
            mock.patch.object(proc.os, "name", "nt"),
            mock.patch.object(proc.shutil, "which", which),
        ):
            self.assertEqual(proc._shell_argv("dir"), ["C:\\tools\\sh.exe", "-c", "dir"])

    def test_windows_falls_back_to_bash(self):
        def which(name):
            return {"bash": "C:\\tools\\bash.exe"}.get(name)  # no 'sh'

        with (
            mock.patch.object(proc.os, "name", "nt"),
            mock.patch.object(proc.shutil, "which", which),
        ):
            self.assertEqual(proc._shell_argv("dir"), ["C:\\tools\\bash.exe", "-c", "dir"])

    def test_windows_falls_back_to_cmd(self):
        with (
            mock.patch.object(proc.os, "name", "nt"),
            mock.patch.object(proc.shutil, "which", lambda _n: None),
        ):
            self.assertEqual(proc._shell_argv("dir"), ["cmd", "/c", "dir"])


# --------------------------------------------------------------------------- #
# run_shell — unit level (popen_tree / kill_tree monkeypatched)
# --------------------------------------------------------------------------- #
class TestRunShellUnit(unittest.TestCase):
    def test_invokes_popen_tree_with_clean_env_and_flags(self):
        rec = _RecordingPopenTree(_FakeHandle(returncode=0, stdout="out"))
        with mock.patch.object(proc, "popen_tree", rec):
            proc.run_shell("echo hi", Path("/tmp"), timeout=11)
        self.assertEqual(len(rec.calls), 1)
        args, kwargs = rec.calls[0]
        self.assertEqual(args[0], proc._shell_argv("echo hi"))
        self.assertIs(kwargs["env"], proc._CLEAN_ENV)
        self.assertEqual(kwargs["cwd"], Path("/tmp"))
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        # The timeout is enforced via handle.communicate(timeout=…), not popen_tree.
        self.assertNotIn("timeout", kwargs)
        self.assertEqual(rec.handle.communicate_calls, [11])

    def test_success_returns_outcome(self):
        rec = _RecordingPopenTree(_FakeHandle(returncode=3, stdout="hello world"))
        with mock.patch.object(proc, "popen_tree", rec):
            outcome = proc.run_shell("whatever", Path("."))
        self.assertEqual(outcome.returncode, 3)
        self.assertEqual(outcome.output, "hello world")
        self.assertFalse(outcome.timed_out)

    def test_none_stdout_becomes_empty_string(self):
        rec = _RecordingPopenTree(_FakeHandle(returncode=0, stdout=None))
        with mock.patch.object(proc, "popen_tree", rec):
            outcome = proc.run_shell("whatever", Path("."))
        self.assertEqual(outcome.output, "")
        self.assertFalse(outcome.timed_out)

    def test_default_timeout_is_none(self):
        rec = _RecordingPopenTree(_FakeHandle(returncode=0, stdout=""))
        with mock.patch.object(proc, "popen_tree", rec):
            proc.run_shell("whatever", Path("."))
        self.assertEqual(rec.handle.communicate_calls, [None])

    def test_timeout_kills_tree_and_returns_message(self):
        # On timeout, run_shell must kill the WHOLE process tree (not just the
        # direct child) and return the timeout message — never re-raise.
        rec = _RecordingPopenTree(_FakeHandle(raise_timeout=True))
        killed = []
        with (
            mock.patch.object(proc, "popen_tree", rec),
            mock.patch.object(proc, "kill_tree", killed.append),
        ):
            outcome = proc.run_shell("sleep 99", Path("."), timeout=5)
        self.assertEqual(killed, [rec.handle], "timeout must route through kill_tree")
        self.assertEqual(outcome.returncode, 124)
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.output, "(command timed out after 5s: sleep 99)")


# --------------------------------------------------------------------------- #
# run_shell — POSIX integration (real /bin/sh)
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_IS_POSIX, "real /bin/sh integration is POSIX-only")
class TestRunShellIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_happy_path_stdout(self):
        outcome = proc.run_shell("echo hello", self.cwd)
        self.assertEqual(outcome.returncode, 0)
        self.assertIn("hello", outcome.output)
        self.assertFalse(outcome.timed_out)

    def test_stderr_is_merged_into_output(self):
        outcome = proc.run_shell("echo oops 1>&2", self.cwd)
        self.assertEqual(outcome.returncode, 0)
        self.assertIn("oops", outcome.output)

    def test_nonzero_exit_code_is_preserved(self):
        outcome = proc.run_shell("exit 7", self.cwd)
        self.assertEqual(outcome.returncode, 7)
        self.assertFalse(outcome.timed_out)

    def test_non_utf8_output_does_not_raise(self):
        outcome = proc.run_shell(r"printf '\377\376'", self.cwd)
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.timed_out)
        self.assertIsInstance(outcome.output, str)

    def test_runs_in_given_cwd(self):
        outcome = proc.run_shell("pwd", self.cwd)
        # macOS /var is a symlink to /private/var; compare resolved paths.
        self.assertEqual(Path(outcome.output.strip()).resolve(), self.cwd.resolve())

    def test_timeout_kills_whole_process_tree(self):
        # Regression (issue #13): a gate/test command that backgrounds a grandchild
        # must have that grandchild killed on timeout, not orphaned. A plain
        # subprocess.run(timeout=) would SIGKILL only the shell and leave `sleep`
        # running.
        pidfile = self.cwd / "grandchild.pid"
        cmd = f"sleep 30 & echo $! > '{pidfile}'; wait"
        outcome = proc.run_shell(cmd, self.cwd, timeout=1)
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.returncode, 124)
        grandchild = int(pidfile.read_text().strip())
        # The backgrounded `sleep 30` must be gone shortly after the timeout kill.
        for _ in range(100):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"grandchild pid {grandchild} survived timeout — process tree not killed")


# --------------------------------------------------------------------------- #
# _popen_group_kwargs
# --------------------------------------------------------------------------- #
class TestPopenGroupKwargs(unittest.TestCase):
    def test_posix_branch(self):
        with mock.patch.object(proc.os, "name", "posix"):
            self.assertEqual(proc._popen_group_kwargs(), {"start_new_session": True})

    def test_windows_branch(self):
        sentinel = 0x00000200
        with (
            mock.patch.object(proc.os, "name", "nt"),
            mock.patch.object(
                proc.subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                sentinel,
                create=True,
            ),
        ):
            self.assertEqual(proc._popen_group_kwargs(), {"creationflags": sentinel})


# --------------------------------------------------------------------------- #
# popen_tree
# --------------------------------------------------------------------------- #
class TestPopenTree(unittest.TestCase):
    def test_calls_module_subprocess_popen(self):
        rec = _RecordingPopen()
        with (
            mock.patch.object(proc.os, "name", "posix"),
            mock.patch.object(proc.subprocess, "Popen", rec),
        ):
            result = proc.popen_tree(["echo", "hi"], stdout=subprocess.PIPE)
        self.assertIs(result, rec.instance)
        self.assertEqual(len(rec.calls), 1)
        args, kwargs = rec.calls[0]
        self.assertEqual(args[0], ["echo", "hi"])
        # caller kwargs preserved
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        # group kwargs always applied (POSIX branch)
        self.assertTrue(kwargs["start_new_session"])

    def test_group_kwargs_override_on_conflict(self):
        rec = _RecordingPopen()
        with (
            mock.patch.object(proc.os, "name", "posix"),
            mock.patch.object(proc.subprocess, "Popen", rec),
        ):
            proc.popen_tree(["x"], start_new_session=False)
        _args, kwargs = rec.calls[0]
        # {**kwargs, **_popen_group_kwargs()} => group flag wins.
        self.assertTrue(kwargs["start_new_session"])


# --------------------------------------------------------------------------- #
# kill_tree
# --------------------------------------------------------------------------- #
class TestKillTree(unittest.TestCase):
    def test_suppresses_lookup_error_and_waits(self):
        # A pid with no process group makes os.getpgid raise ProcessLookupError,
        # which must be suppressed; wait() must still be called.
        fake = _RecordingProc(pid=999_999_999)
        with mock.patch.object(proc.os, "name", "posix"):
            proc.kill_tree(fake)  # must not raise
        self.assertGreaterEqual(fake.waited, 1)

    def test_windows_uses_taskkill(self):
        fake = _RecordingProc(pid=4242)
        rec = _RecordingRun(result=_FakeCompleted(0, ""))
        with (
            mock.patch.object(proc.os, "name", "nt"),
            mock.patch.object(proc.subprocess, "run", rec),
        ):
            proc.kill_tree(fake)
        self.assertEqual(len(rec.calls), 1)
        args, _kwargs = rec.calls[0]
        self.assertEqual(args[0], ["taskkill", "/F", "/T", "/PID", "4242"])
        self.assertGreaterEqual(fake.waited, 1)

    @unittest.skipUnless(_IS_POSIX, "real process-group kill is POSIX-only")
    def test_real_posix_kill_terminates_process(self):
        child = proc.popen_tree(
            ["/bin/sh", "-c", "sleep 30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.kill_tree(child)
        # kill_tree calls proc.wait() internally, so poll() is now resolved.
        self.assertIsNotNone(child.poll())


# --------------------------------------------------------------------------- #
# Module hygiene / import constraints (source-level)
# --------------------------------------------------------------------------- #
class TestModuleHygiene(unittest.TestCase):
    def setUp(self):
        self.src = _PROC_SRC.read_text()
        self.tree = ast.parse(self.src)

    def test_imports_clean_env_from_runtime(self):
        self.assertIs(proc._CLEAN_ENV, provider._CLEAN_ENV)

    def test_only_director_runtime_clean_env_imported(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "director" or mod.startswith("director."):
                    self.assertEqual(mod, "director.provider")
                    names = {a.name for a in node.names}
                    self.assertEqual(names, {"_CLEAN_ENV"})
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("director"))

    def test_no_platform_only_attrs_at_module_top_level(self):
        forbidden = {"killpg", "CREATE_NEW_PROCESS_GROUP", "getpgid"}
        for stmt in self.tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for node in ast.walk(stmt):
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden)
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, forbidden)

    def test_module_subprocess_is_real_subprocess(self):
        self.assertIs(proc.subprocess, subprocess)


if __name__ == "__main__":
    unittest.main()
