"""Acceptance tests for director.claudecode (headless Claude Code provider).

These tests exercise the parsing/aggregation logic and the command-building /
subprocess-driving logic of `director.claudecode` in isolation. No real `claude`
CLI, network, or model is invoked.

`run_claude` no longer calls `subprocess.Popen` directly: it starts the child in
its own process group/session and force-kills the whole tree on timeout by
delegating to the shared helpers `director.proc.popen_tree` / `kill_tree`, which
`claudecode` imports as `proc_mod`. The subprocess-driving tests therefore patch
that delegation seam (`director.claudecode.proc_mod.popen_tree` and
`...proc_mod.kill_tree`) rather than `claudecode.subprocess.Popen`.

Run: python3 -m unittest discover -s tests -p test_claudecode.py -q
"""

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

from director.claudecode import (  # noqa: E402
    _parse_claude,
    run_claude,
    system_prompt_for,
)
from director.opencode import RunResult  # noqa: E402


# --------------------------------------------------------------------------- #
# system_prompt_for
# --------------------------------------------------------------------------- #
class _FakeFile:
    def __init__(self, text):
        self._text = text

    def read_text(self):
        return self._text


class _FakeDir:
    def __init__(self, mapping):
        self.mapping = mapping

    def joinpath(self, name):
        return self.mapping[name]


class TestSystemPromptFor(unittest.TestCase):
    def test_planner_strips_frontmatter(self):
        body = system_prompt_for("planner")
        # frontmatter delimiters and keys are gone
        self.assertNotIn("---", body.splitlines())
        self.assertNotIn("description:", body)
        self.assertNotIn("mode: all", body)
        # the real body begins with the role declaration
        self.assertTrue(body.startswith("You are the **planner**"))
        # no leading/trailing whitespace
        self.assertEqual(body, body.strip())

    def test_test_author_underscore_to_hyphen(self):
        # test_author -> test-author.md
        body = system_prompt_for("test_author")
        self.assertNotIn("---", body.splitlines())
        self.assertNotIn("description:", body)
        self.assertTrue(len(body) > 0)

    def test_executor_template(self):
        body = system_prompt_for("executor")
        self.assertNotIn("---", body.splitlines())
        self.assertNotIn("description:", body)
        self.assertEqual(body, body.strip())

    def test_no_frontmatter_returned_stripped(self):
        import director.claudecode as cc

        fake = _FakeDir({"plain.md": _FakeFile("just some body text\n")})
        orig = cc.ir.files
        cc.ir.files = lambda *a, **k: fake
        try:
            self.assertEqual(system_prompt_for("plain"), "just some body text")
        finally:
            cc.ir.files = orig

    def test_frontmatter_with_leading_whitespace(self):
        import director.claudecode as cc

        fake = _FakeDir(
            {
                "ws.md": _FakeFile("\n\n---\nkey: val\nmode: all\n---\nbody here\n"),
            }
        )
        orig = cc.ir.files
        cc.ir.files = lambda *a, **k: fake
        try:
            self.assertEqual(system_prompt_for("ws"), "body here")
        finally:
            cc.ir.files = orig

    def test_frontmatter_only_first_block_stripped(self):
        # a `---` later in the body must NOT be treated as a closer
        import director.claudecode as cc

        text = "---\nkey: val\n---\nintro\n\n---\n\nmore body\n"
        fake = _FakeDir({"multi.md": _FakeFile(text)})
        orig = cc.ir.files
        cc.ir.files = lambda *a, **k: fake
        try:
            body = system_prompt_for("multi")
            self.assertTrue(body.startswith("intro"))
            self.assertIn("---", body)  # the second --- survives in the body
        finally:
            cc.ir.files = orig


# --------------------------------------------------------------------------- #
# _parse_claude
# --------------------------------------------------------------------------- #
class TestParseClaude(unittest.TestCase):
    def _write(self, tmp, name, payload):
        p = Path(tmp) / name
        p.write_text(payload)
        return p

    def test_single_json_object_with_result(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"result": "hello world"}))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertIsInstance(r, RunResult)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.text, "hello world")
            self.assertFalse(r.timed_out)
            self.assertIsNone(r.error)
            self.assertEqual(r.log_path, str(p))

    def test_ndjson_stream(self):
        with tempfile.TemporaryDirectory() as d:
            payload = "\n".join(
                [
                    json.dumps({"type": "system", "subtype": "init"}),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "part1 "}]},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "part2"}]},
                        }
                    ),
                    "not json at all",
                    json.dumps({"result": " tail "}),
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.text, "part1 part2 tail")

    def test_message_content_as_string(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {"type": "assistant", "message": {"content": "raw string content"}}
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.text, "raw string content")

    def test_tokens_top_level_usage_with_cache_keys(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                        "reasoning": 3,
                        "total_tokens": 168,
                    }
                }
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tokens["input"], 100)
            self.assertEqual(r.tokens["output"], 50)
            self.assertEqual(r.tokens["cache_read"], 10)
            self.assertEqual(r.tokens["cache_write"], 5)
            self.assertEqual(r.tokens["reasoning"], 3)
            # reported total used verbatim
            self.assertEqual(r.tokens["total"], 168)

    def test_tokens_cache_not_added_to_input(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 999,
                        "cache_creation_input_tokens": 888,
                    }
                }
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tokens["input"], 100)
            self.assertEqual(r.tokens["cache_read"], 999)
            self.assertEqual(r.tokens["cache_write"], 888)
            # no reported total -> input + output
            self.assertEqual(r.tokens["total"], 100)

    def test_tokens_from_message_usage(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {"input_tokens": 7, "output_tokens": 4},
                        "content": [{"type": "text", "text": "hi"}],
                    },
                }
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tokens["input"], 7)
            self.assertEqual(r.tokens["output"], 4)
            self.assertEqual(r.tokens["total"], 11)

    def test_tokens_aggregate_across_records(self):
        with tempfile.TemporaryDirectory() as d:
            payload = "\n".join(
                [
                    json.dumps({"usage": {"input_tokens": 10, "output_tokens": 1}}),
                    json.dumps({"usage": {"input_tokens": 5, "output_tokens": 2}}),
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tokens["input"], 15)
            self.assertEqual(r.tokens["output"], 3)
            self.assertEqual(r.tokens["total"], 18)

    def test_tokens_missing_usage_defaults_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"result": "ok"}))
            r = _parse_claude(p, rc=0, timed_out=False)
            for k in ("input", "output", "reasoning", "cache_read", "cache_write", "total"):
                self.assertEqual(r.tokens[k], 0)

    def test_cost_reported(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"total_cost_usd": 0.0123}))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertAlmostEqual(r.cost_reported, 0.0123)

    def test_cost_default_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"result": "ok"}))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.cost_reported, 0.0)

    def test_n_steps_from_num_turns(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"num_turns": 3, "result": "done"}))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.n_steps, 3)

    def test_n_steps_zero_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"result": "done"}))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.n_steps, 0)

    def test_n_steps_from_assistant_count(self):
        with tempfile.TemporaryDirectory() as d:
            payload = "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "a"}]},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "b"}]},
                        }
                    ),
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.n_steps, 2)

    def test_tool_calls_top_level_record(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps({"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}})
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tool_calls, [("Bash", "?")])
            self.assertEqual(len(r.tool_events), 1)
            ev = r.tool_events[0]
            self.assertEqual(ev["name"], "bash")
            self.assertEqual(ev["status"], "?")
            self.assertIn("blob", ev)
            self.assertIsInstance(ev["blob"], str)
            # blob is the json-dumped, lowercased, capped part
            self.assertIn("bash", ev["blob"])

    def test_tool_calls_with_status_and_state(self):
        with tempfile.TemporaryDirectory() as d:
            payload = "\n".join(
                [
                    json.dumps({"type": "tool_use", "name": "Bash", "status": "ok"}),
                    json.dumps({"type": "tool_result", "tool": "Read", "state": "done"}),
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tool_calls, [("Bash", "ok"), ("Read", "done")])
            self.assertEqual(r.tool_events[0]["name"], "bash")
            self.assertEqual(r.tool_events[0]["status"], "ok")
            self.assertEqual(r.tool_events[1]["name"], "read")
            self.assertEqual(r.tool_events[1]["status"], "done")

    def test_tool_calls_within_message_content(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "thinking"},
                            {"type": "tool_use", "name": "Edit", "input": {"file": "a.py"}},
                        ],
                    },
                }
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tool_calls, [("Edit", "?")])
            self.assertEqual(r.tool_events[0]["name"], "edit")

    def test_no_tool_records_empty_lists(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"result": "plain text"}))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.tool_calls, [])
            self.assertEqual(r.tool_events, [])

    def test_error_is_error_flag(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps({"is_error": True, "result": "boom"})
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertIsNotNone(r.error)
            self.assertIsInstance(r.error, str)
            self.assertTrue(len(r.error) > 0)

    def test_error_field(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps({"type": "error", "error": "API rate limited"})
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertIsNotNone(r.error)
            self.assertIsInstance(r.error, str)

    def test_error_rc_nonzero_no_result(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"type": "system", "subtype": "init"}))
            r = _parse_claude(p, rc=1, timed_out=False)
            self.assertIsNotNone(r.error)
            self.assertIsInstance(r.error, str)

    def test_no_error_when_rc_nonzero_but_result_present(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"result": "partial output"}))
            r = _parse_claude(p, rc=1, timed_out=False)
            self.assertIsNone(r.error)

    def test_no_error_clean_run(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", json.dumps({"result": "all good"}))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertIsNone(r.error)

    def test_timed_out_propagates(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", "")
            r = _parse_claude(p, rc=124, timed_out=True)
            self.assertTrue(r.timed_out)
            self.assertEqual(r.returncode, 124)

    def test_empty_log_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", "")
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.text, "")
            self.assertEqual(r.tokens["total"], 0)
            self.assertEqual(r.tool_calls, [])
            self.assertIsNone(r.error)

    def test_garbage_lines_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            payload = "\n".join(
                [
                    "garbage",
                    "",
                    "{not valid json",
                    json.dumps({"result": "good"}),
                    "trailing junk",
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(r.text, "good")

    def test_blob_capped_at_2000(self):
        with tempfile.TemporaryDirectory() as d:
            big = {"type": "tool_use", "name": "Bash", "input": {"cmd": "x" * 5000}}
            p = self._write(d, "out.log", json.dumps(big))
            r = _parse_claude(p, rc=0, timed_out=False)
            self.assertEqual(len(r.tool_events), 1)
            self.assertLessEqual(len(r.tool_events[0]["blob"]), 2000)


# --------------------------------------------------------------------------- #
# run_claude — delegates spawning/killing to director.proc (proc_mod)
# --------------------------------------------------------------------------- #
class _FakeHandle:
    """Stand-in for the Popen handle returned by proc.popen_tree."""

    def __init__(self, rc=0, raise_timeout=False, pid=4321):
        self._rc = rc
        self._raise_timeout = raise_timeout
        self.pid = pid
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        # Only the first wait (the timeout-bounded one) times out; once the tree
        # has been killed the process is reaped, so later waits return the code.
        if self._raise_timeout and len(self.waits) == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return self._rc


class _PopenTreeFactory:
    """Records the popen_tree call and writes payload/err to the given streams."""

    def __init__(self, payload=b"", err=b"", rc=0, raise_timeout=False):
        self.payload = payload
        self.err = err
        self.rc = rc
        self.raise_timeout = raise_timeout
        self.calls = 0
        self.last_cmd = None
        self.last_cwd = None
        self.last_env = None
        self.last_stdout = None
        self.last_stderr = None
        self.last_extra_kwargs = None
        self.handle = None

    def __call__(self, cmd, cwd=None, stdout=None, stderr=None, env=None, **extra):
        self.calls += 1
        self.last_cmd = list(cmd)
        self.last_cwd = cwd
        self.last_env = env
        self.last_stdout = stdout
        self.last_stderr = stderr
        self.last_extra_kwargs = dict(extra)
        if stdout is not None:
            stdout.write(self.payload)
            stdout.flush()
        if stderr is not None:
            stderr.write(self.err)
            stderr.flush()
        self.handle = _FakeHandle(rc=self.rc, raise_timeout=self.raise_timeout)
        return self.handle


class _RecordingKill:
    """Records every kill_tree(handle) call; optionally raises to test best-effort."""

    def __init__(self, raise_exc=None):
        self.calls = []
        self.raise_exc = raise_exc

    def __call__(self, handle):
        self.calls.append(handle)
        if self.raise_exc is not None:
            raise self.raise_exc


class TestRunClaudeDelegatesToProc(unittest.TestCase):
    """The callsite contract: run_claude routes through proc_mod, not raw Popen."""

    def _run(self, factory, kill=None, **kwargs):
        import director.claudecode as cc

        kill = _RecordingKill() if kill is None else kill
        # Patching attributes on cc.proc_mod evaluates cc.proc_mod first; before
        # the callsite is wired this raises AttributeError -> the test goes red
        # for the right reason (the proc_mod delegation is missing).
        with (
            mock.patch.object(cc.proc_mod, "popen_tree", factory),
            mock.patch.object(cc.proc_mod, "kill_tree", kill),
        ):
            self._last_kill = kill
            return run_claude(**kwargs)

    def test_proc_mod_alias_is_the_proc_module(self):
        import director.claudecode as cc

        self.assertTrue(hasattr(cc, "proc_mod"), "claudecode must import `proc as proc_mod`")
        import director.proc as proc_module

        self.assertIs(cc.proc_mod, proc_module)

    def test_routes_through_popen_tree_not_raw_popen(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="opus",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            # popen_tree was the spawning seam, called exactly once.
            self.assertEqual(factory.calls, 1)

    def test_builds_correct_command(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "logs" / "run.json"
            payload = json.dumps({"result": "ok"}).encode()
            factory = _PopenTreeFactory(payload=payload, rc=0)
            r = self._run(
                factory,
                agent="planner",
                model="opus",
                message="do the thing",
                cwd=d,
                log_path=log_path,
                timeout=30,
            )
            cmd = factory.last_cmd
            self.assertEqual(cmd[0], "claude")
            self.assertIn("-p", cmd)
            self.assertIn("do the thing", cmd)
            self.assertIn("--output-format", cmd)
            self.assertIn("json", cmd)
            self.assertIn("--model", cmd)
            # run_claude receives the already-stripped model and passes it through;
            # run_agent strips the "claude-code/" prefix upstream.
            model_idx = cmd.index("--model") + 1
            self.assertEqual(cmd[model_idx], "opus")
            self.assertIn("--append-system-prompt", cmd)
            body_idx = cmd.index("--append-system-prompt") + 1
            # body is the planner system prompt (frontmatter stripped)
            self.assertTrue(cmd[body_idx].startswith("You are the **planner**"))
            self.assertIn("--dangerously-skip-permissions", cmd)
            # result parsed
            self.assertEqual(r.text, "ok")
            self.assertEqual(r.returncode, 0)

    def test_uses_clean_env(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="anthropic/claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertIsNotNone(factory.last_env)
            # popen_tree is handed _CLEAN_ENV verbatim (the same object claudecode
            # imports from director.provider).
            from director.provider import _CLEAN_ENV

            self.assertIs(factory.last_env, _CLEAN_ENV)
            # PYTHONDONTWRITEBYTECODE is popped; byproducts handled by the gate's ignore matcher
            self.assertNotIn("PYTHONDONTWRITEBYTECODE", factory.last_env)

    def test_creates_log_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "deep" / "nested" / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertTrue(log_path.parent.exists())
            self.assertTrue(log_path.exists())

    def test_writes_stderr_file(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", err=b"some stderr", rc=0)
            self._run(
                factory,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            err_path = log_path.with_suffix(log_path.suffix + ".stderr")
            self.assertTrue(err_path.exists())
            self.assertEqual(err_path.read_bytes(), b"some stderr")

    def test_cwd_passed_to_popen_tree(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            # cwd is passed through stringified, exactly as the old Popen call did.
            self.assertEqual(factory.last_cwd, str(d))

    def test_parses_written_payload(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            payload = json.dumps(
                {
                    "result": "final answer",
                    "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
                    "total_cost_usd": 0.05,
                    "num_turns": 4,
                }
            ).encode()
            factory = _PopenTreeFactory(payload=payload, rc=0)
            r = self._run(
                factory,
                agent="planner",
                model="openrouter/anthropic/claude-opus-4.8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertEqual(r.text, "final answer")
            self.assertEqual(r.tokens["input"], 12)
            self.assertEqual(r.tokens["output"], 8)
            self.assertEqual(r.tokens["total"], 20)
            self.assertAlmostEqual(r.cost_reported, 0.05)
            self.assertEqual(r.n_steps, 4)
            self.assertIsNone(r.error)
            self.assertEqual(r.log_path, str(log_path))

    def test_no_kill_tree_on_clean_run(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            # Happy path must not force-kill anything.
            self.assertEqual(self._last_kill.calls, [])

    def test_timeout_sets_rc_124_and_timed_out(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"", rc=0, raise_timeout=True)
            r = self._run(
                factory,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=1,
            )
            self.assertEqual(r.returncode, 124)
            self.assertTrue(r.timed_out)

    def test_timeout_force_kills_the_tree(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"", rc=0, raise_timeout=True)
            self._run(
                factory,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=1,
            )
            # kill_tree was called exactly once with the SAME handle popen_tree
            # returned (the whole tree, not a bare proc.kill()).
            self.assertEqual(self._last_kill.calls, [factory.handle])

    def test_kill_path_is_best_effort(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"", rc=0, raise_timeout=True)
            # kill_tree blowing up must not escape run_claude.
            kill = _RecordingKill(raise_exc=OSError("boom"))
            r = self._run(
                factory,
                kill=kill,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=1,
            )
            self.assertEqual(len(kill.calls), 1)
            self.assertEqual(r.returncode, 124)
            self.assertTrue(r.timed_out)

    def test_never_raises_on_cli_failure(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            # rc=2, empty payload -> error surfaced via RunResult, not raised
            factory = _PopenTreeFactory(payload=b"", rc=2)
            r = self._run(
                factory,
                agent="planner",
                model="claude-opus-4-8",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertEqual(r.returncode, 2)
            self.assertIsNotNone(r.error)
            # A non-timeout failure must not force-kill.
            self.assertEqual(self._last_kill.calls, [])


if __name__ == "__main__":
    unittest.main()
