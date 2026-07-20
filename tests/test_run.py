"""Acceptance tests for the callsite-run node.

Pins the contract of `director.run._run_shell` after it is rewired to delegate to
the shared `director.proc.run_shell` helper:

  - Behavioral: `_run_shell(cmd, cwd, timeout)` calls `proc.run_shell(cmd, cwd,
    timeout)` positionally and returns its `.output` attribute (a `str`).
  - Signature/return contract is unchanged: (cmd, cwd, timeout) -> str.
  - Source hygiene: `run.py` imports `proc` from `director`, the `_run_shell` body
    no longer touches `subprocess`/`os` (no inline clean-env), and there is no
    `subprocess.run(..., shell=True, ...)` left anywhere in the module.
  - Untouched neighbour: `tempfile.gettempdir()` usage stays.

No real network, time, or randomness is used. The behavioral test injects a
recording fake for `proc.run_shell` so no real process is spawned on the green
path.

Run: python -m unittest tests.test_run -v
"""

import ast
import contextlib
import inspect
import os
import pathlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.run as run  # noqa: E402

_RUN_SRC_PATH = pathlib.Path(__file__).resolve().parent.parent / "director" / "run.py"


# --------------------------------------------------------------------------- #
# Test doubles / helpers
# --------------------------------------------------------------------------- #
class _RunShellRecorder:
    """Records calls and returns a ShellOutcome-like object exposing `.output`."""

    def __init__(self, output):
        self._output = output
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0, output=self._output, timed_out=False)


@contextlib.contextmanager
def _patched_proc_run_shell(fake):
    """Patch `director.run.proc.run_shell` regardless of import-time wiring.

    If the delegation does not exist yet, `run` has no `proc` attribute; we add a
    throwaway namespace so the old (non-delegating) body still runs and the
    assertions fail meaningfully rather than erroring on a missing attribute.
    """
    created = not hasattr(run, "proc")
    if created:
        run.proc = types.SimpleNamespace()
    try:
        with mock.patch.object(run.proc, "run_shell", fake, create=True):
            yield
    finally:
        if created:
            delattr(run, "proc")


# --------------------------------------------------------------------------- #
# Behavioral delegation
# --------------------------------------------------------------------------- #
class TestRunShellDelegation(unittest.TestCase):
    def test_returns_proc_output_attribute(self):
        fake = _RunShellRecorder(output="DELEGATED-OUTPUT")
        with _patched_proc_run_shell(fake):
            result = run._run_shell("echo should-not-appear", Path("/tmp"), 5)
        self.assertEqual(result, "DELEGATED-OUTPUT")

    def test_forwards_args_positionally(self):
        # cwd is an existing dir and the command is benign so that the pre-delegation
        # body (which really shells out) fails the assertions cleanly rather than
        # raising on a missing path/command.
        fake = _RunShellRecorder(output="ok")
        cwd = Path(".")
        with _patched_proc_run_shell(fake):
            run._run_shell("echo forwards", cwd, 42)
        self.assertEqual(len(fake.calls), 1)
        args, kwargs = fake.calls[0]
        self.assertEqual(args, ("echo forwards", cwd, 42))
        self.assertEqual(kwargs, {})

    def test_distinct_timeout_is_passed_through(self):
        fake = _RunShellRecorder(output="ok")
        with _patched_proc_run_shell(fake):
            run._run_shell("echo timeout", Path("."), 1234)
        self.assertEqual(len(fake.calls), 1)
        args, _kwargs = fake.calls[0]
        self.assertEqual(args[2], 1234)

    def test_result_is_a_str(self):
        fake = _RunShellRecorder(output="line1\nline2")
        with _patched_proc_run_shell(fake):
            result = run._run_shell("cmd", Path("."), 5)
        self.assertIsInstance(result, str)


# --------------------------------------------------------------------------- #
# Signature / return contract (must stay EXACTLY the same)
# --------------------------------------------------------------------------- #
class TestRunShellSignature(unittest.TestCase):
    def test_parameter_names_and_order(self):
        sig = inspect.signature(run._run_shell)
        self.assertEqual(list(sig.parameters), ["cmd", "cwd", "timeout"])

    def test_return_annotation_is_str(self):
        sig = inspect.signature(run._run_shell)
        # `from __future__ import annotations` makes annotations strings.
        self.assertEqual(sig.return_annotation, "str")

    def test_param_annotations(self):
        params = inspect.signature(run._run_shell).parameters
        self.assertEqual(params["cmd"].annotation, "str")
        self.assertEqual(params["cwd"].annotation, "Path")
        self.assertEqual(params["timeout"].annotation, "int")


# --------------------------------------------------------------------------- #
# Source hygiene (the rewire actually happened, and nothing else changed)
# --------------------------------------------------------------------------- #
class TestRunSource(unittest.TestCase):
    def setUp(self):
        self.src = _RUN_SRC_PATH.read_text()
        self.tree = ast.parse(self.src)

    def _run_shell_def(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_run_shell":
                return node
        self.fail("_run_shell function not found in director/run.py")

    def test_no_shell_true_anywhere(self):
        self.assertNotIn("shell=True", self.src)

    def test_imports_proc_from_director(self):
        imported = False
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "director" and any(a.name == "proc" for a in node.names):
                    imported = True
                if node.module == "director.proc" and any(
                    a.name == "run_shell" for a in node.names
                ):
                    imported = True
        self.assertTrue(
            imported,
            "run.py must import `proc` from director (or run_shell from director.proc)",
        )

    def test_run_shell_body_has_no_subprocess_or_os(self):
        fn = self._run_shell_def()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                self.assertNotIn(
                    node.id,
                    {"subprocess", "os"},
                    "delegated _run_shell must not reference subprocess/os",
                )

    def test_run_shell_body_calls_run_shell(self):
        fn = self._run_shell_def()
        names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            if isinstance(node, ast.Name):
                names.add(node.id)
        self.assertIn("run_shell", names)
        self.assertIn("output", names)

    def test_gettempdir_usage_preserved(self):
        # The temp-dir worktree root moved behind JobContext (issue #38): run.py
        # asks the context, and the legacy context still uses tempfile.gettempdir().
        self.assertIn("ctx.worktree_root(plan.job_id)", self.src)
        jobctx_src = (
            pathlib.Path(__file__).resolve().parent.parent / "director" / "jobctx.py"
        ).read_text()
        self.assertIn("tempfile.gettempdir()", jobctx_src)


if __name__ == "__main__":
    unittest.main()
