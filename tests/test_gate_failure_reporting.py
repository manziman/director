"""Regression tests for concise, attributable node-gate failures."""

from __future__ import annotations

import os
import pathlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import gates, run  # noqa: E402
from director.models import Node  # noqa: E402


class GateFailureSummaryTests(unittest.TestCase):
    def test_preserves_command_and_head_of_long_output(self):
        output = "first actionable error\n" + "x" * 3_000 + "\nTAIL-MARKER"

        detail = gates.gate_failure_detail("verify project", output)

        self.assertTrue(detail.startswith("$ verify project\nfirst actionable error"))
        self.assertIn("[output truncated]", detail)
        self.assertNotIn("TAIL-MARKER", detail)

    def test_reports_diagnostic_outside_allowlist(self):
        node = Node("n", "n", "s", ["src/owned.ts"], test_cmd="check")
        output = "src/unrelated.test.ts:12:7: error: broken baseline\n"

        summary = gates.gate_failure_summary(node, output)

        self.assertIn("outside this node's allowlist", summary)
        self.assertIn("src/unrelated.test.ts", summary)
        self.assertIn("$ check", summary)

    def test_reports_diagnostic_inside_allowlist_as_node_failure(self):
        node = Node("n", "n", "s", ["src/owned.ts"], test_cmd="check")
        output = "src/owned.ts(12,7): error: broken change\n"

        summary = gates.gate_failure_summary(node, output)

        self.assertIn("within this node's allowlist", summary)
        self.assertNotIn("project-wide", summary)

    def test_reports_unparseable_gate_output_as_unknown_scope(self):
        node = Node("n", "n", "s", ["src/owned.ts"], test_cmd="check")

        summary = gates.gate_failure_summary(node, "verification failed\n")

        self.assertIn("no identifiable diagnostic file paths", summary)
        self.assertIn("project-wide or unknown-scope", summary)

    def test_node_gate_exposes_summary_for_state_reporting(self):
        node = Node("n", "n", "s", ["src/owned.ts"], test_cmd="check")
        cfg = type("Config", (), {"node_timeout": 1, "flake_runs": 1})()
        output = "src/unrelated.ts:3:1: error: broken baseline\n"

        with mock.patch.object(gates, "_run", return_value=(1, output)):
            result = gates.node_gate(node, Path("."), cfg)

        self.assertFalse(result.ok)
        self.assertIn("outside this node's allowlist", result.summary)
        self.assertTrue(result.detail.startswith("$ check\n"))

    def test_exhausted_node_uses_gate_summary_for_persisted_error(self):
        node = Node("n", "n", "s", ["src/owned.ts"], test_cmd="check")
        cfg = type(
            "Config",
            (),
            {"node_timeout": 1, "model_for": lambda _self, _role: "provider/model"},
        )()
        summary = "gate reported diagnostics outside this node's allowlist. $ check: first error"
        agent_result = types.SimpleNamespace(
            tokens={"input": 0, "output": 0},
            tool_events=[],
            error=None,
            timed_out=False,
        )
        gate_result = gates.GateResult(False, ["node tests"], "$ check\nfirst error", summary)

        with (
            mock.patch.object(run, "run_agent", return_value=agent_result),
            mock.patch.object(run, "node_gate", return_value=gate_result),
        ):
            outcome = run._process_node(node, Path("."), cfg, Path("."), 0, lambda _msg: None)

        self.assertEqual(outcome.error, f"exhausted: {summary}")


if __name__ == "__main__":
    unittest.main()
