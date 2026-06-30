"""Acceptance tests for the callsite-plan node.

Pins the contract of `director.plan._run_shell` after it is rewired to delegate to
the shared `director.proc.run_shell` helper while preserving its NO-TIMEOUT
behavior:

  - Behavioral: `_run_shell(cmd, cwd)` calls `proc.run_shell(cmd, cwd, timeout=None)`
    (cmd/cwd positional, timeout forwarded as None) and returns its `.returncode`
    attribute (an `int`).
  - Signature/return contract is unchanged: (cmd, cwd) -> int, with NO timeout
    parameter on the helper.
  - Source hygiene: `plan.py` imports `proc` from `director`, the `_run_shell` body
    no longer touches `subprocess`/`os` (no inline clean-env), and there is no
    `subprocess.run(..., shell=True, ...)` left anywhere in the module.
  - Untouched neighbour: the single caller `_run_shell(n.test_cmd, repo) == 0` stays.

No real network, time, or randomness is used. The behavioral test injects a
recording fake for `proc.run_shell` so no real process is spawned on the green
path.

Run: python -m unittest tests.test_plan -v
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

import director.plan as plan  # noqa: E402

_PLAN_SRC_PATH = pathlib.Path(__file__).resolve().parent.parent / "director" / "plan.py"


# --------------------------------------------------------------------------- #
# Test doubles / helpers
# --------------------------------------------------------------------------- #
class _RunShellRecorder:
    """Records calls and returns a ShellOutcome-like object exposing `.returncode`."""

    def __init__(self, returncode):
        self._returncode = returncode
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=self._returncode, output="", timed_out=False)


def _timeout_of(args, kwargs):
    """Return the timeout value forwarded to proc.run_shell, kw or positional."""
    if "timeout" in kwargs:
        return kwargs["timeout"]
    if len(args) >= 3:
        return args[2]
    raise AssertionError("timeout argument was not forwarded to proc.run_shell")


@contextlib.contextmanager
def _patched_proc_run_shell(fake):
    """Patch `director.plan.proc.run_shell` regardless of import-time wiring.

    If the delegation does not exist yet, `plan` has no `proc` attribute; we add a
    throwaway namespace so the old (non-delegating) body still runs and the
    assertions fail meaningfully rather than erroring on a missing attribute.
    """
    created = not hasattr(plan, "proc")
    if created:
        plan.proc = types.SimpleNamespace()
    try:
        with mock.patch.object(plan.proc, "run_shell", fake, create=True):
            yield
    finally:
        if created:
            delattr(plan, "proc")


# --------------------------------------------------------------------------- #
# Behavioral delegation
# --------------------------------------------------------------------------- #
class TestRunShellDelegation(unittest.TestCase):
    def test_returns_proc_returncode_attribute(self):
        fake = _RunShellRecorder(returncode=77)
        with _patched_proc_run_shell(fake):
            result = plan._run_shell("echo should-not-appear", Path("."))
        self.assertEqual(result, 77)

    def test_forwards_cmd_and_cwd_positionally(self):
        # cwd is an existing dir and the command is benign so the pre-delegation
        # body (which really shells out) fails the assertions cleanly rather than
        # raising on a missing path/command.
        fake = _RunShellRecorder(returncode=0)
        cwd = Path(".")
        with _patched_proc_run_shell(fake):
            plan._run_shell("echo forwards", cwd)
        self.assertEqual(len(fake.calls), 1)
        args, _kwargs = fake.calls[0]
        self.assertEqual(args[0], "echo forwards")
        self.assertEqual(args[1], cwd)

    def test_timeout_is_forwarded_as_none(self):
        # The helper must KEEP having no timeout: it always delegates with None.
        fake = _RunShellRecorder(returncode=0)
        with _patched_proc_run_shell(fake):
            plan._run_shell("echo timeout", Path("."))
        self.assertEqual(len(fake.calls), 1)
        args, kwargs = fake.calls[0]
        self.assertIsNone(_timeout_of(args, kwargs))

    def test_result_is_an_int(self):
        fake = _RunShellRecorder(returncode=3)
        with _patched_proc_run_shell(fake):
            result = plan._run_shell("cmd", Path("."))
        self.assertIsInstance(result, int)


# --------------------------------------------------------------------------- #
# Signature / return contract (must stay EXACTLY the same — NO timeout param)
# --------------------------------------------------------------------------- #
class TestRunShellSignature(unittest.TestCase):
    def test_parameter_names_and_order(self):
        sig = inspect.signature(plan._run_shell)
        self.assertEqual(list(sig.parameters), ["cmd", "cwd"])

    def test_no_timeout_parameter(self):
        sig = inspect.signature(plan._run_shell)
        self.assertNotIn("timeout", sig.parameters)

    def test_return_annotation_is_int(self):
        sig = inspect.signature(plan._run_shell)
        # `from __future__ import annotations` makes annotations strings.
        self.assertEqual(sig.return_annotation, "int")

    def test_param_annotations(self):
        params = inspect.signature(plan._run_shell).parameters
        self.assertEqual(params["cmd"].annotation, "str")
        self.assertEqual(params["cwd"].annotation, "Path")


# --------------------------------------------------------------------------- #
# Source hygiene (the rewire actually happened, and nothing else changed)
# --------------------------------------------------------------------------- #
class TestPlanSource(unittest.TestCase):
    def setUp(self):
        self.src = _PLAN_SRC_PATH.read_text()
        self.tree = ast.parse(self.src)

    def _run_shell_def(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_run_shell":
                return node
        self.fail("_run_shell function not found in director/plan.py")

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
            "plan.py must import `proc` from director (or run_shell from director.proc)",
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

    def test_run_shell_body_calls_run_shell_and_uses_returncode(self):
        fn = self._run_shell_def()
        names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            if isinstance(node, ast.Name):
                names.add(node.id)
        self.assertIn("run_shell", names)
        self.assertIn("returncode", names)

    def test_single_caller_preserved(self):
        self.assertIn("_run_shell(n.test_cmd, repo)", self.src)


if __name__ == "__main__":
    unittest.main()
