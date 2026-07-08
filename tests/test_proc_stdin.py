"""Acceptance tests for the proc.py stdin hardening (node: proc-stdin-hardening).

Agent child processes must never inherit director's stdin — otherwise an
OpenCode stdin-probe hangs and exits 124. These tests pin two guarantees:

  CHANGE 1 — run_shell(...) passes stdin=subprocess.DEVNULL to popen_tree(...),
             leaving every other existing kwarg unchanged.
  CHANGE 2 — popen_tree(cmd, **kwargs) DEFAULTS stdin to subprocess.DEVNULL, but
             a caller-supplied `stdin` in kwargs still wins.

No real processes are spawned: subprocess.Popen / popen_tree are monkeypatched on
the module's own references. Deterministic; no time/network/randomness.

Run: python -m unittest tests.test_proc_stdin -v
"""

import importlib
import os
import pathlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import proc, provider  # noqa: E402


def setUpModule():
    """Refresh proc in case other suites reloaded director.provider first."""
    importlib.reload(provider)
    importlib.reload(proc)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _RecordingPopen:
    """Captures the args/kwargs of a single subprocess.Popen call."""

    def __init__(self):
        self.calls = []
        self.instance = object()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.instance


class _FakeHandle:
    """Minimal popen_tree handle: communicate() returns (stdout, None)."""

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self._stdout = stdout
        self.pid = 4242

    def communicate(self, timeout=None):
        return (self._stdout, None)


class _RecordingPopenTree:
    """Captures the args/kwargs of a single popen_tree call; returns a handle."""

    def __init__(self, handle):
        self.handle = handle
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.handle


# --------------------------------------------------------------------------- #
# CHANGE 1 — run_shell must pass stdin=DEVNULL to popen_tree
# --------------------------------------------------------------------------- #
class TestRunShellStdinDevNull(unittest.TestCase):
    def test_run_shell_passes_stdin_devnull_to_popen_tree(self):
        rec = _RecordingPopenTree(_FakeHandle(returncode=0, stdout="out"))
        with mock.patch.object(proc, "popen_tree", rec):
            proc.run_shell("echo hi", Path("/tmp"), timeout=11)
        self.assertEqual(len(rec.calls), 1)
        _args, kwargs = rec.calls[0]
        self.assertIn("stdin", kwargs, "run_shell must pass stdin explicitly")
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_run_shell_preserves_all_other_kwargs(self):
        # Adding stdin must be purely additive: the existing contract stays intact.
        rec = _RecordingPopenTree(_FakeHandle(returncode=0, stdout="out"))
        with mock.patch.object(proc, "popen_tree", rec):
            proc.run_shell("echo hi", Path("/tmp"), timeout=11)
        args, kwargs = rec.calls[0]
        self.assertEqual(args[0], proc._shell_argv("echo hi"))
        self.assertIs(kwargs["env"], proc._CLEAN_ENV)
        self.assertEqual(kwargs["cwd"], Path("/tmp"))
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertNotIn("timeout", kwargs)


# --------------------------------------------------------------------------- #
# CHANGE 2 — popen_tree defaults stdin to DEVNULL, caller override wins
# --------------------------------------------------------------------------- #
class TestPopenTreeStdinDefault(unittest.TestCase):
    def test_default_stdin_is_devnull_when_caller_omits_it(self):
        rec = _RecordingPopen()
        with (
            mock.patch.object(proc.os, "name", "posix"),
            mock.patch.object(proc.subprocess, "Popen", rec),
        ):
            proc.popen_tree(["echo", "hi"], stdout=subprocess.PIPE)
        _args, kwargs = rec.calls[0]
        self.assertIs(
            kwargs["stdin"],
            subprocess.DEVNULL,
            "popen_tree must default stdin to DEVNULL when caller omits it",
        )

    def test_caller_supplied_stdin_overrides_default(self):
        # A caller passing an explicit stdin (e.g. a pipe) must keep it.
        sentinel_pipe = object()
        rec = _RecordingPopen()
        with (
            mock.patch.object(proc.os, "name", "posix"),
            mock.patch.object(proc.subprocess, "Popen", rec),
        ):
            proc.popen_tree(["cat"], stdin=sentinel_pipe)
        _args, kwargs = rec.calls[0]
        self.assertIs(
            kwargs["stdin"],
            sentinel_pipe,
            "caller-supplied stdin must win over the DEVNULL default",
        )

    def test_caller_stdin_pipe_constant_overrides_default(self):
        rec = _RecordingPopen()
        with (
            mock.patch.object(proc.os, "name", "posix"),
            mock.patch.object(proc.subprocess, "Popen", rec),
        ):
            proc.popen_tree(["cat"], stdin=subprocess.PIPE)
        _args, kwargs = rec.calls[0]
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)

    def test_stdin_default_does_not_clobber_group_kwargs(self):
        # The DEVNULL default must coexist with the group-isolation kwargs.
        rec = _RecordingPopen()
        with (
            mock.patch.object(proc.os, "name", "posix"),
            mock.patch.object(proc.subprocess, "Popen", rec),
        ):
            proc.popen_tree(["echo", "hi"], stdout=subprocess.PIPE)
        _args, kwargs = rec.calls[0]
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
