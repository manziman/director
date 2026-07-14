"""Offline contract tests for the Pi provider adapter."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.pi as pi  # noqa: E402
from director.provider import RunResult  # noqa: E402


def _write(tmp: str, records: list[dict] | str) -> Path:
    path = Path(tmp) / "pi.jsonl"
    if isinstance(records, str):
        path.write_text(records)
    else:
        path.write_text("\n".join(json.dumps(record) for record in records))
    return path


class TestParsePi(unittest.TestCase):
    def test_agent_end_prefers_latest_assistant_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                [
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "old"}],
                        },
                    },
                    {
                        "type": "agent_end",
                        "messages": [
                            {"role": "assistant", "content": [{"type": "text", "text": "final"}]}
                        ],
                    },
                ],
            )
            result = pi._parse_pi(path, 0, False)
            self.assertEqual(result.text, "final")
            self.assertEqual(result.returncode, 0)

    def test_malformed_unknown_and_tool_events_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                "bad line\n"
                + json.dumps({"type": "unknown", "value": object.__name__})
                + "\n"
                + json.dumps(
                    {
                        "type": "tool_execution_start",
                        "toolCallId": "1",
                        "toolName": "bash",
                        "args": {"command": "ls"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolCallId": "1",
                        "toolName": "bash",
                        "result": "ok",
                        "isError": False,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "write",
                        "result": {"error": "denied"},
                        "isError": True,
                    }
                ),
            )
            result = pi._parse_pi(path, 0, False)
            self.assertEqual(result.tool_calls, [("bash", "completed"), ("write", "failed")])
            self.assertEqual(len(result.tool_events), 3)
            self.assertIn("denied", result.error or "")

    def test_tool_result_error_and_retry_errors_are_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                [
                    {
                        "type": "tool_execution_end",
                        "toolName": "bash",
                        "result": {"error": "denied"},
                    },
                    {"type": "auto_retry_end", "success": False, "finalError": "retry exhausted"},
                    {"type": "extension_error", "message": "extension broke"},
                ],
            )
            result = pi._parse_pi(path, 0, False)
            self.assertEqual(result.tool_calls, [("bash", "failed")])
            for diagnostic in ("denied", "retry exhausted", "extension broke"):
                self.assertIn(diagnostic, result.error or "")

    def test_false_tool_error_flag_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                [
                    {
                        "type": "tool_execution_end",
                        "toolName": "bash",
                        "result": {"isError": False, "text": "ok"},
                    }
                ],
            )
            result = pi._parse_pi(path, 0, False)
            self.assertEqual(result.tool_calls, [("bash", "completed")])
            self.assertIsNone(result.error)

    def test_message_stop_reason_and_error_message_are_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                [
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "stopReason": "error",
                            "errorMessage": "provider down",
                        },
                    }
                ],
            )
            result = pi._parse_pi(path, 0, False)
            self.assertIn("provider down", result.error or "")

    def test_usage_variants_and_cost_are_numeric_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                [
                    {
                        "type": "turn_end",
                        "message": {
                            "usage": {
                                "inputTokens": 12,
                                "outputTokens": 8,
                                "cacheRead": 2,
                                "cacheWrite": 1,
                                "totalTokens": 20,
                                "cost": {"total": 0.25},
                            }
                        },
                    }
                ],
            )
            result = pi._parse_pi(path, 0, False)
            self.assertEqual(
                result.tokens,
                {
                    "input": 12,
                    "output": 8,
                    "reasoning": 0,
                    "cache_read": 2,
                    "cache_write": 1,
                    "total": 20,
                },
            )
            self.assertEqual(result.cost_reported, 0.25)

    def test_overlapping_event_costs_are_not_double_counted(self):
        usage = {"input": 2, "output": 1, "totalTokens": 3, "cost": {"total": 0.25}}
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                [
                    {"type": "message_end", "message": {"role": "assistant", "usage": usage}},
                    {"type": "turn_end", "message": {"role": "assistant", "usage": usage}},
                    {"type": "agent_end", "messages": [{"role": "assistant", "usage": usage}]},
                ],
            )
            result = pi._parse_pi(path, 0, False)
            self.assertEqual(result.tokens["total"], 3)
            self.assertEqual(result.cost_reported, 0.25)

    def test_plain_output_and_nonzero_exit_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "plain output")
            result = pi._parse_pi(path, 3, False)
            self.assertEqual(result.text, "plain output")
            self.assertIn("non-zero exit code 3", result.error or "")

    def test_timeout_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = pi._parse_pi(_write(tmp, ""), 124, True)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.returncode, 124)


class _Handle:
    pid = 42

    def wait(self, timeout=None):
        self.timeout = timeout
        return 0


class TestRunPi(unittest.TestCase):
    def test_command_environment_and_streams(self):
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured.update(cmd=cmd, **kwargs)
            kwargs["stdout"].write(
                b'{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}\n'
            )
            return _Handle()

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(pi.proc_mod, "popen_tree", fake_popen),
        ):
            result = pi.run_pi(
                agent="planner",
                model="anthropic/claude-sonnet-4-5",
                message="do the task",
                cwd=tmp,
                log_path=Path(tmp) / "nested" / "run.jsonl",
                timeout=7,
            )
        self.assertEqual(
            captured["cmd"][:7],
            [
                "pi",
                "-p",
                "--mode",
                "json",
                "--no-session",
                "--model",
                "anthropic/claude-sonnet-4-5",
            ],
        )
        self.assertIn("--append-system-prompt", captured["cmd"])
        self.assertEqual(captured["cmd"][-1], "do the task")
        self.assertEqual(captured["env"]["PI_SKIP_VERSION_CHECK"], "1")
        self.assertEqual(captured["env"]["PI_TELEMETRY"], "0")
        self.assertEqual(result.text, "ok")

    def test_timeout_kills_process_tree(self):
        handle = _Handle()

        def fake_wait(timeout=None):
            raise subprocess.TimeoutExpired("pi", timeout)

        handle.wait = fake_wait
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(pi.proc_mod, "popen_tree", return_value=handle),
            mock.patch.object(pi.proc_mod, "kill_tree") as kill,
        ):
            result = pi.run_pi(
                agent="executor",
                model="openai/gpt-5.5",
                message="m",
                cwd=tmp,
                log_path=Path(tmp) / "run.jsonl",
                timeout=1,
            )
        kill.assert_called_once_with(handle)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)


class TestPiProvider(unittest.TestCase):
    def test_strips_only_first_provider_segment(self):
        with mock.patch.object(pi, "run_pi", return_value=RunResult()) as run:
            pi.PiProvider().run(
                agent="executor",
                model="pi/anthropic/claude-sonnet-4-5",
                message="m",
                cwd=".",
                log_path="x",
                timeout=1,
            )
        self.assertEqual(run.call_args.kwargs["model"], "anthropic/claude-sonnet-4-5")

    def test_bare_model_is_unchanged_and_discovery_is_empty(self):
        with mock.patch.object(pi, "run_pi", return_value=RunResult()) as run:
            pi.PiProvider().run(
                agent="executor",
                model="claude-sonnet-4-5",
                message="m",
                cwd=".",
                log_path="x",
                timeout=1,
            )
        self.assertEqual(run.call_args.kwargs["model"], "claude-sonnet-4-5")
        self.assertEqual(pi.PiProvider().discover_models(), [])


if __name__ == "__main__":
    unittest.main()
