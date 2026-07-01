"""Acceptance tests for director.codex (headless OpenAI Codex CLI provider).

These tests exercise the parsing/aggregation logic (`_parse_codex`), the
command-building / subprocess-driving logic (`run_codex`), and the Provider
adapter (`CodexProvider`) in isolation. No real `codex` CLI, network, or model
is invoked.

`run_codex` mirrors `run_claude`: it starts the child through the shared
`director.proc.popen_tree` / `kill_tree` helpers (imported by `codex` as
`proc_mod`) and force-kills the whole tree on timeout. The subprocess-driving
tests therefore patch that delegation seam
(`director.codex.proc_mod.popen_tree` / `...proc_mod.kill_tree`).

Both this parser AND its fixtures are written against the FIXED "assumed Codex
output contract" in the node spec — recognized object shapes keyed by `type`:
  message:   {"type":"message","role":"assistant","content":"<text>"}
  tool_call: {"type":"tool_call","name":"<tool>","status":"<status>"}
  usage:     {"type":"usage","input_tokens":int,"output_tokens":int,
              "reasoning_tokens":int,"cache_read_tokens":int,
              "cache_write_tokens":int,"total_tokens":int}
  cost:      {"type":"cost","amount":float}
  error:     {"type":"error","message":"<msg>"}

Run: python3 -m unittest discover -s tests -p test_codex.py -q
"""

import inspect
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

import director.codex as codex_mod  # noqa: E402
from director.codex import (  # noqa: E402
    CODEX_MODELS,
    CodexProvider,
    _parse_codex,
    run_codex,
)
from director.provider import RunResult, resolve  # noqa: E402


# --------------------------------------------------------------------------- #
# _parse_codex — shape-tolerant parser
# --------------------------------------------------------------------------- #
class TestParseCodex(unittest.TestCase):
    def _write(self, tmp, name, payload):
        p = Path(tmp) / name
        p.write_text(payload)
        return p

    def test_single_json_object_message(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {"type": "message", "role": "assistant", "content": "hello world"}
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertIsInstance(r, RunResult)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.text, "hello world")
            self.assertFalse(r.timed_out)
            self.assertIsNone(r.error)
            self.assertEqual(r.log_path, str(p))

    def test_json_array_accumulates_content(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                [
                    {"type": "message", "role": "assistant", "content": "part1 "},
                    {"type": "message", "role": "assistant", "content": "part2"},
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.text, "part1 part2")

    def test_ndjson_stream_accumulates_content(self):
        with tempfile.TemporaryDirectory() as d:
            payload = "\n".join(
                [
                    json.dumps({"type": "message", "role": "assistant", "content": "a "}),
                    "not json at all",
                    json.dumps({"type": "message", "role": "assistant", "content": "b"}),
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.text, "a b")

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", "")
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.text, "")
            self.assertEqual(r.tokens["total"], 0)
            self.assertEqual(r.tool_calls, [])
            self.assertEqual(r.tool_events, [])
            self.assertIsNone(r.error)

    def test_malformed_json_degrades_to_raw_text(self):
        # single json.loads fails; the '{'-prefixed line also fails to parse ->
        # no records at all -> raw-stdout-as-text degradation.
        with tempfile.TemporaryDirectory() as d:
            raw = '{"type": "message", "content": '  # truncated / malformed
            p = self._write(d, "out.log", raw)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.text, raw.strip())
            self.assertEqual(r.tool_calls, [])

    def test_plain_non_json_text_is_raw_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            raw = "just some plain output\nsecond line of text\n"
            p = self._write(d, "out.log", raw)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.text, raw.strip())

    def test_usage_tokens_mapped_from_codex_field_names(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {
                    "type": "usage",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "reasoning_tokens": 7,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 5,
                    "total_tokens": 172,
                }
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.tokens["input"], 100)
            self.assertEqual(r.tokens["output"], 50)
            self.assertEqual(r.tokens["reasoning"], 7)
            self.assertEqual(r.tokens["cache_read"], 10)
            self.assertEqual(r.tokens["cache_write"], 5)
            self.assertEqual(r.tokens["total"], 172)

    def test_usage_total_defaults_to_input_plus_output(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {"type": "usage", "input_tokens": 12, "output_tokens": 8}
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.tokens["input"], 12)
            self.assertEqual(r.tokens["output"], 8)
            self.assertEqual(r.tokens["total"], 20)

    def test_usage_missing_keys_default_zero(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {"type": "message", "role": "assistant", "content": "hi"}
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            for k in ("input", "output", "reasoning", "cache_read", "cache_write", "total"):
                self.assertEqual(r.tokens[k], 0)

    def test_cost_reported(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps({"type": "cost", "amount": 0.0123})
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertAlmostEqual(r.cost_reported, 0.0123)

    def test_tool_call_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {"type": "tool_call", "name": "shell", "status": "completed"}
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.tool_calls, [("shell", "completed")])
            self.assertEqual(r.n_steps, 1)
            self.assertEqual(len(r.tool_events), 1)
            ev = r.tool_events[0]
            self.assertEqual(ev["name"], "shell")
            self.assertEqual(ev["status"], "completed")

    def test_multiple_tool_calls_counted_in_n_steps(self):
        with tempfile.TemporaryDirectory() as d:
            payload = "\n".join(
                [
                    json.dumps({"type": "tool_call", "name": "shell", "status": "ok"}),
                    json.dumps({"type": "tool_call", "name": "edit", "status": "ok"}),
                    json.dumps(
                        {"type": "message", "role": "assistant", "content": "done"}
                    ),
                ]
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.tool_calls, [("shell", "ok"), ("edit", "ok")])
            self.assertEqual(r.n_steps, 2)
            self.assertEqual(r.text, "done")

    def test_error_object_surfaces_message(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps({"type": "error", "message": "API rate limited"})
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 0, False)
            self.assertEqual(r.error, "API rate limited")

    def test_timeout_direct_call(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "out.log", "")
            r = _parse_codex(p, 124, True)
            self.assertEqual(r.returncode, 124)
            self.assertTrue(r.timed_out)

    def test_nonzero_exit_with_no_text_synthesizes_error(self):
        with tempfile.TemporaryDirectory() as d:
            # empty output + rc!=0 and no error object -> synthesized error
            p = self._write(d, "out.log", "")
            r = _parse_codex(p, 3, False)
            self.assertEqual(r.returncode, 3)
            self.assertEqual(r.error, "non-zero exit code 3")

    def test_nonzero_exit_with_text_keeps_error_none(self):
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps(
                {"type": "message", "role": "assistant", "content": "partial"}
            )
            p = self._write(d, "out.log", payload)
            r = _parse_codex(p, 1, False)
            self.assertIsNone(r.error)

    def test_never_raises_on_bad_input(self):
        nasty = [
            "",
            "   \n\t  ",
            "not json",
            "{",
            "{broken",
            "[1, 2, 3]",  # array of non-dicts
            "null",
            "12345",
            '{"type": "unknown_shape"}',
            "\n".join(["garbage", "{still bad", "plain"]),
        ]
        with tempfile.TemporaryDirectory() as d:
            for i, raw in enumerate(nasty):
                p = Path(d) / f"bad_{i}.log"
                p.write_text(raw)
                try:
                    r = _parse_codex(p, 0, False)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"_parse_codex raised on input {raw!r}: {exc!r}")
                self.assertIsInstance(r, RunResult)


# --------------------------------------------------------------------------- #
# run_codex — delegates spawning/killing to director.proc (proc_mod)
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
        if self._raise_timeout and len(self.waits) == 1:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
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
        self.handle = None

    def __call__(self, cmd, cwd=None, stdout=None, stderr=None, env=None, **extra):
        self.calls += 1
        self.last_cmd = list(cmd)
        self.last_cwd = cwd
        self.last_env = env
        self.last_stdout = stdout
        self.last_stderr = stderr
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


class TestRunCodexDelegatesToProc(unittest.TestCase):
    """run_codex must route through proc_mod, not raw Popen — mirrors run_claude."""

    def _run(self, factory, kill=None, **kwargs):
        kill = _RecordingKill() if kill is None else kill
        with (
            mock.patch.object(codex_mod.proc_mod, "popen_tree", factory),
            mock.patch.object(codex_mod.proc_mod, "kill_tree", kill),
        ):
            self._last_kill = kill
            return run_codex(**kwargs)

    def test_proc_mod_alias_is_the_proc_module(self):
        self.assertTrue(
            hasattr(codex_mod, "proc_mod"), "codex must import `proc as proc_mod`"
        )
        import director.proc as proc_module

        self.assertIs(codex_mod.proc_mod, proc_module)

    def test_routes_through_popen_tree_once(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertEqual(factory.calls, 1)

    def test_builds_codex_exec_command(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "logs" / "run.json"
            payload = json.dumps(
                {"type": "message", "role": "assistant", "content": "ok"}
            ).encode()
            factory = _PopenTreeFactory(payload=payload, rc=0)
            r = self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
                message="do the thing",
                cwd=d,
                log_path=log_path,
                timeout=30,
            )
            cmd = factory.last_cmd
            # subcommand `codex exec`
            self.assertEqual(cmd[0], "codex")
            self.assertIn("exec", cmd)
            # model passed via -m / --model (already prefix-stripped by caller)
            self.assertTrue(
                "-m" in cmd or "--model" in cmd, "model must be passed via -m/--model"
            )
            flag = "-m" if "-m" in cmd else "--model"
            self.assertEqual(cmd[cmd.index(flag) + 1], "gpt-5-codex")
            # prompt is the LAST positional argument (possibly with sp preamble)
            self.assertTrue(
                cmd[-1].endswith("do the thing"),
                f"message must be the last positional arg; got {cmd[-1]!r}",
            )
            # system prompt is injected somewhere in the command (either via a
            # -c config value or prepended into the positional prompt)
            self.assertIn("You are the **planner**", " ".join(cmd))
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
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            from director.provider import _CLEAN_ENV

            self.assertIs(factory.last_env, _CLEAN_ENV)
            self.assertNotIn("PYTHONDONTWRITEBYTECODE", factory.last_env)

    def test_creates_log_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "deep" / "nested" / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertTrue(log_path.parent.exists())
            self.assertTrue(log_path.exists())

    def test_writes_stderr_sibling_file(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", err=b"some stderr", rc=0)
            self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            # same sibling expression run_claude uses
            err_path = log_path.with_suffix(log_path.suffix + ".stderr")
            self.assertTrue(err_path.exists())
            self.assertEqual(err_path.read_bytes(), b"some stderr")

    def test_cwd_passed_stringified(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertEqual(factory.last_cwd, str(d))

    def test_no_kill_tree_on_clean_run(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"{}", rc=0)
            self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertEqual(self._last_kill.calls, [])

    def test_timeout_sets_rc_124_and_timed_out(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"", rc=0, raise_timeout=True)
            r = self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
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
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=1,
            )
            self.assertEqual(self._last_kill.calls, [factory.handle])

    def test_kill_path_is_best_effort(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "run.json"
            factory = _PopenTreeFactory(payload=b"", rc=0, raise_timeout=True)
            kill = _RecordingKill(raise_exc=OSError("boom"))
            r = self._run(
                factory,
                kill=kill,
                agent="planner",
                model="gpt-5-codex",
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
            factory = _PopenTreeFactory(payload=b"", rc=2)
            r = self._run(
                factory,
                agent="planner",
                model="gpt-5-codex",
                message="m",
                cwd=d,
                log_path=log_path,
                timeout=5,
            )
            self.assertEqual(r.returncode, 2)
            self.assertIsNotNone(r.error)
            self.assertEqual(self._last_kill.calls, [])

    def test_signature_is_keyword_only(self):
        sig = inspect.signature(run_codex)
        self.assertEqual(
            list(sig.parameters),
            ["agent", "model", "message", "cwd", "log_path", "timeout"],
        )
        for p in sig.parameters.values():
            self.assertEqual(
                p.kind,
                inspect.Parameter.KEYWORD_ONLY,
                f"parameter {p.name!r} must be keyword-only",
            )


# --------------------------------------------------------------------------- #
# CodexProvider — Provider protocol adapter
# --------------------------------------------------------------------------- #
class TestCodexProvider(unittest.TestCase):
    def test_name(self):
        self.assertEqual(CodexProvider.name, "codex")

    def test_run_strips_prefix_and_delegates_to_module_level_run_codex(self):
        recorder = {}

        def fake_run_codex(**kwargs):
            recorder.update(kwargs)
            return RunResult(returncode=0, text="ok")

        # Monkeypatching the bare module-level name proves run() looks it up by
        # name (not a captured reference).
        with mock.patch.object(codex_mod, "run_codex", fake_run_codex):
            r = CodexProvider().run(
                agent="planner",
                model="codex/gpt-5-codex",
                message="m",
                cwd="/tmp",
                log_path="/tmp/x.json",
                timeout=5,
            )
        self.assertIsInstance(r, RunResult)
        self.assertEqual(recorder["model"], "gpt-5-codex")
        self.assertEqual(recorder["agent"], "planner")
        self.assertEqual(recorder["message"], "m")

    def test_run_passes_bare_model_through_unchanged(self):
        recorder = {}

        def fake_run_codex(**kwargs):
            recorder.update(kwargs)
            return RunResult()

        with mock.patch.object(codex_mod, "run_codex", fake_run_codex):
            CodexProvider().run(
                agent="planner",
                model="gpt-5-codex",  # no "codex/" prefix
                message="m",
                cwd="/tmp",
                log_path="/tmp/x.json",
                timeout=5,
            )
        self.assertEqual(recorder["model"], "gpt-5-codex")

    def test_system_prompt_for_delegates_to_loader(self):
        from director.claudecode import system_prompt_for as loader

        self.assertEqual(
            CodexProvider().system_prompt_for("planner"), loader("planner")
        )

    def test_discover_models_returns_prefixed_aliases(self):
        self.assertEqual(CodexProvider().discover_models(), ["codex/gpt-5-codex"])

    def test_discover_models_never_raises(self):
        # Force iteration over CODEX_MODELS to blow up; must degrade to [].
        with mock.patch.object(codex_mod, "CODEX_MODELS", 123):
            try:
                result = CodexProvider().discover_models()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"discover_models raised {type(exc).__name__}: {exc}")
        self.assertEqual(result, [])

    def test_codex_models_constant(self):
        self.assertEqual(CODEX_MODELS, ("gpt-5-codex",))

    def test_provider_registered(self):
        prov = resolve("codex")
        self.assertIsNotNone(prov, "CodexProvider must be register()ed at import")
        self.assertEqual(prov.name, "codex")


if __name__ == "__main__":
    unittest.main()
