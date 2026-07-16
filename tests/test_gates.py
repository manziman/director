"""Acceptance tests for the callsite-gates node.

Pins the contract of `director.gates._run` after it is rewired to delegate to the
shared `director.proc.run_shell` helper:

  - Behavioral: `_run(cmd, cwd, timeout)` calls `proc.run_shell(cmd, cwd, timeout)`
    positionally and returns `(o.returncode, o.output)` on success, or
    `(124, "(gate command timed out after {timeout}s: {cmd})")` when `o.timed_out`.
  - The gate-specific timeout message string is preserved EXACTLY (note the literal
    word "gate"); the helper builds its own message and does NOT surface
    `proc.run_shell`'s `.output` on the timeout branch.
  - Signature/return contract is unchanged: (cmd, cwd, timeout) -> tuple[int, str].
  - Source hygiene: `gates.py` imports `proc` from `director`, the `_run` body no
    longer references `subprocess`/`_CLEAN_ENV` (no inline clean-env), and there is
    no `subprocess.run(..., shell=True, ...)` left anywhere in the module.

No real network, time, or randomness is used. Behavioral tests inject a recording
fake for `proc.run_shell` so no real process is spawned on the green path.

Run: python -m unittest tests.test_gates -v
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

import director.gates as gates  # noqa: E402

_GATES_SRC_PATH = pathlib.Path(__file__).resolve().parent.parent / "director" / "gates.py"


# --------------------------------------------------------------------------- #
# Test doubles / helpers
# --------------------------------------------------------------------------- #
class _RunShellRecorder:
    """Records calls and returns a ShellOutcome-like object.

    Exposes `.returncode`, `.output`, and `.timed_out` so the delegating body can
    branch on `o.timed_out` and read `o.returncode` / `o.output`.
    """

    def __init__(self, returncode=0, output="", timed_out=False):
        self._returncode = returncode
        self._output = output
        self._timed_out = timed_out
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return types.SimpleNamespace(
            returncode=self._returncode,
            output=self._output,
            timed_out=self._timed_out,
        )


@contextlib.contextmanager
def _patched_proc_run_shell(fake):
    """Patch `director.gates.proc.run_shell` regardless of import-time wiring.

    If the delegation does not exist yet, `gates` has no `proc` attribute; we add a
    throwaway namespace so the old (non-delegating) body still runs and the
    assertions fail meaningfully rather than erroring on a missing attribute.
    """
    created = not hasattr(gates, "proc")
    if created:
        gates.proc = types.SimpleNamespace()
    try:
        with mock.patch.object(gates.proc, "run_shell", fake, create=True):
            yield
    finally:
        if created:
            delattr(gates, "proc")


# --------------------------------------------------------------------------- #
# Behavioral delegation
# --------------------------------------------------------------------------- #
class TestRunDelegation(unittest.TestCase):
    def test_success_returns_returncode_and_output(self):
        fake = _RunShellRecorder(returncode=7, output="DELEGATED-OUTPUT")
        with _patched_proc_run_shell(fake):
            result = gates._run("echo should-not-appear", Path("."), 5)
        self.assertEqual(result, (7, "DELEGATED-OUTPUT"))

    def test_forwards_args_positionally(self):
        fake = _RunShellRecorder(returncode=0, output="ok")
        cwd = Path(".")
        with _patched_proc_run_shell(fake):
            gates._run("echo forwards", cwd, 42)
        self.assertEqual(len(fake.calls), 1)
        args, kwargs = fake.calls[0]
        self.assertEqual(args, ("echo forwards", cwd, 42))
        self.assertEqual(kwargs, {})

    def test_zero_returncode_passthrough(self):
        fake = _RunShellRecorder(returncode=0, output="all good")
        with _patched_proc_run_shell(fake):
            result = gates._run("echo x", Path("."), 5)
        self.assertEqual(result, (0, "all good"))

    def test_result_is_tuple_int_str(self):
        fake = _RunShellRecorder(returncode=3, output="line1\nline2")
        with _patched_proc_run_shell(fake):
            result = gates._run("cmd", Path("."), 5)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], int)
        self.assertIsInstance(result[1], str)


# --------------------------------------------------------------------------- #
# Timeout branch — preserves the gate-specific message EXACTLY
# --------------------------------------------------------------------------- #
class TestRunTimeout(unittest.TestCase):
    def test_timeout_returns_124_and_gate_message(self):
        # The fake reports a timeout on a fast/benign command so the test stays
        # deterministic and quick (no real sleep).
        fake = _RunShellRecorder(returncode=99, output="PROC-INTERNAL", timed_out=True)
        with _patched_proc_run_shell(fake):
            rc, out = gates._run("echo hi", Path("."), 7)
        self.assertEqual(rc, 124)
        self.assertEqual(out, "(gate command timed out after 7s: echo hi)")

    def test_timeout_message_ignores_proc_output(self):
        fake = _RunShellRecorder(returncode=99, output="PROC-INTERNAL", timed_out=True)
        with _patched_proc_run_shell(fake):
            _rc, out = gates._run("echo hi", Path("."), 7)
        self.assertNotIn("PROC-INTERNAL", out)

    def test_timeout_interpolates_timeout_and_cmd(self):
        fake = _RunShellRecorder(timed_out=True)
        with _patched_proc_run_shell(fake):
            rc, out = gates._run("pytest -q", Path("."), 1234)
        self.assertEqual(rc, 124)
        self.assertEqual(out, "(gate command timed out after 1234s: pytest -q)")


# --------------------------------------------------------------------------- #
# Signature / return contract (must stay EXACTLY the same)
# --------------------------------------------------------------------------- #
class TestRunSignature(unittest.TestCase):
    def test_parameter_names_and_order(self):
        sig = inspect.signature(gates._run)
        self.assertEqual(list(sig.parameters), ["cmd", "cwd", "timeout"])

    def test_return_annotation_is_tuple_int_str(self):
        sig = inspect.signature(gates._run)
        # `from __future__ import annotations` makes annotations strings.
        self.assertEqual(sig.return_annotation, "tuple[int, str]")

    def test_param_annotations(self):
        params = inspect.signature(gates._run).parameters
        self.assertEqual(params["cmd"].annotation, "str")
        self.assertEqual(params["cwd"].annotation, "Path")
        self.assertEqual(params["timeout"].annotation, "int")


# --------------------------------------------------------------------------- #
# Source hygiene (the rewire actually happened, and nothing else changed)
# --------------------------------------------------------------------------- #
class TestGatesSource(unittest.TestCase):
    def setUp(self):
        self.src = _GATES_SRC_PATH.read_text()
        self.tree = ast.parse(self.src)

    def _run_def(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_run":
                return node
        self.fail("_run function not found in director/gates.py")

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
            "gates.py must import `proc` from director (or run_shell from director.proc)",
        )

    def test_run_body_has_no_subprocess_or_clean_env(self):
        fn = self._run_def()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                self.assertNotIn(
                    node.id,
                    {"subprocess", "_CLEAN_ENV"},
                    "delegated _run must not reference subprocess or the inline clean-env",
                )
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(
                    node.attr,
                    "run",
                    "delegated _run must not call subprocess.run",
                )

    def test_run_body_calls_run_shell_and_reads_output(self):
        fn = self._run_def()
        names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            if isinstance(node, ast.Name):
                names.add(node.id)
        self.assertIn("run_shell", names)
        self.assertIn("output", names)
        self.assertIn("timed_out", names)


class TestUserDefinedIntegrationGates(unittest.TestCase):
    def test_configured_gates_create_reusable_gate_values(self):
        configured = gates.configured_gates(
            {"security": " scan ", "disabled": "  ", "verify": "check"}
        )
        self.assertIsInstance(configured, tuple)
        self.assertEqual(
            configured,
            (
                gates.Gate(name="security", command="scan"),
                gates.Gate(name="verify", command="check"),
            ),
        )

    def test_generic_runner_accepts_gate_values_without_config(self):
        configured = (
            gates.Gate(name="security", command="scan"),
            gates.Gate(name="verify", command="check"),
        )
        with mock.patch.object(gates, "_run", side_effect=[(0, "ok"), (7, "bad")]) as run:
            result = gates.run_gates(configured, Path("/repo"), timeout=42)

        self.assertEqual(result.failures, ["verify"])
        self.assertIn("$ check\nbad", result.detail)
        self.assertEqual(run.call_count, 2)

    def test_runs_configured_gates_in_order_and_reports_configured_name(self):
        cfg = types.SimpleNamespace(
            node_timeout=42,
            gates={
                "security": "  scan --strict  ",
                "disabled": "   ",
                "verify": "check-all",
            },
        )
        with mock.patch.object(gates, "_run", side_effect=[(0, "ok"), (7, "bad")]) as run:
            result = gates.integration_gate(Path("/repo"), cfg)

        self.assertEqual(
            run.call_args_list,
            [
                mock.call("scan --strict", Path("/repo"), 42),
                mock.call("check-all", Path("/repo"), 42),
            ],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failures, ["verify"])
        self.assertIn("$ check-all\nbad", result.detail)

    def test_zero_configured_gates_succeeds_without_running_commands(self):
        cfg = types.SimpleNamespace(node_timeout=42, gates={})
        with mock.patch.object(gates, "_run") as run:
            result = gates.integration_gate(Path("/repo"), cfg)

        self.assertTrue(result.ok)
        self.assertEqual(result.failures, [])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
