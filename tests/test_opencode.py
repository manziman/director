"""Acceptance tests for OpenCodeProvider.discover_models.

Covers:
  - Method existence and correct signature on OpenCodeProvider
  - Happy path: provider/model lines are extracted and returned in order
  - Filtering: empty lines and lines without '/' are dropped
  - Order: output order is preserved
  - Graceful degradation: FileNotFoundError, CalledProcessError,
    TimeoutExpired, and arbitrary exceptions all yield []
  - subprocess.run is invoked with the expected arguments
  - No import from director.init (self-contained implementation)

Run: python3 -m unittest tests.test_opencode -v
"""

import ast
import contextlib
import importlib
import inspect
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.opencode as oc

_OPENCODE_SRC = pathlib.Path(__file__).resolve().parent.parent / "director" / "opencode.py"


def setUpModule():
    """Refresh OpenCode adapter after provider registry isolation tests reload provider."""
    import director.claudecode as cc
    import director.codex as codex
    import director.provider as provider

    importlib.reload(provider)
    importlib.reload(cc)
    importlib.reload(oc)
    importlib.reload(codex)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fake_run(stdout: str):
    """Return a fake subprocess.CompletedProcess with the given stdout."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = 0
    return result


def _runtime():
    return oc.OpenCodeProvider()


# --------------------------------------------------------------------------- #
# 1. Method existence and signature
# --------------------------------------------------------------------------- #


class TestDiscoverModelsExists(unittest.TestCase):
    def test_opencode_runtime_has_discover_models(self):
        self.assertTrue(
            hasattr(oc.OpenCodeProvider, "discover_models"),
            "OpenCodeProvider must have a discover_models attribute",
        )

    def test_discover_models_is_callable(self):
        self.assertTrue(
            callable(getattr(oc.OpenCodeProvider, "discover_models", None)),
            "OpenCodeProvider.discover_models must be callable",
        )

    def test_discover_models_takes_only_self(self):
        sig = inspect.signature(oc.OpenCodeProvider.discover_models)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params,
            ["self"],
            "discover_models must accept only 'self' — no extra parameters",
        )


# --------------------------------------------------------------------------- #
# 2. Return type
# --------------------------------------------------------------------------- #


class TestDiscoverModelsReturnType(unittest.TestCase):
    def test_returns_list(self):
        with patch("director.opencode.subprocess.run", return_value=_fake_run("")):
            result = _runtime().discover_models()
        self.assertIsInstance(result, list, "discover_models must return a list")

    def test_returns_list_of_strings(self):
        stdout = "anthropic/claude-opus-4-8\nopenai/gpt-4o\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        for item in result:
            self.assertIsInstance(
                item, str, f"discover_models result items must be str, got {type(item)}"
            )


# --------------------------------------------------------------------------- #
# 3. Happy path — correct extraction and ordering
# --------------------------------------------------------------------------- #


class TestDiscoverModelsHappyPath(unittest.TestCase):
    def test_single_model_line_returned(self):
        stdout = "anthropic/claude-sonnet-4-6\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["opencode/anthropic/claude-sonnet-4-6"])

    def test_multiple_model_lines_returned(self):
        stdout = "anthropic/claude-opus-4-8\nopenai/gpt-4o\nGoogle/gemini-2.5-pro\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(
            result,
            [
                "opencode/anthropic/claude-opus-4-8",
                "opencode/openai/gpt-4o",
                "opencode/Google/gemini-2.5-pro",
            ],
        )

    def test_order_is_preserved(self):
        models = [f"provider{i}/model{i}" for i in range(5)]
        stdout = "\n".join(models) + "\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, [f"opencode/{model}" for model in models])

    def test_deeply_nested_model_id_kept(self):
        # e.g. "google-vertex/us-central1/gemini-2.0-flash"
        stdout = "google-vertex/us-central1/gemini-2.0-flash\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["opencode/google-vertex/us-central1/gemini-2.0-flash"])


# --------------------------------------------------------------------------- #
# 4. Filtering: empty lines and lines without '/'
# --------------------------------------------------------------------------- #


class TestDiscoverModelsFiltering(unittest.TestCase):
    def test_empty_lines_dropped(self):
        stdout = "\nanthropic/claude-opus-4-8\n\n\nopenai/gpt-4o\n\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["opencode/anthropic/claude-opus-4-8", "opencode/openai/gpt-4o"])

    def test_lines_without_slash_dropped(self):
        stdout = "Available models:\nanthropic/claude-sonnet-4-6\nsome-bare-token\nopenai/gpt-4o\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertNotIn("Available models:", result)
        self.assertNotIn("some-bare-token", result)
        self.assertIn("opencode/anthropic/claude-sonnet-4-6", result)
        self.assertIn("opencode/openai/gpt-4o", result)

    def test_whitespace_only_lines_dropped(self):
        stdout = "anthropic/claude-opus-4-8\n   \n\t\nopenai/gpt-4o\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["opencode/anthropic/claude-opus-4-8", "opencode/openai/gpt-4o"])

    def test_lines_are_stripped(self):
        stdout = "  anthropic/claude-opus-4-8  \n  openai/gpt-4o  \n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        # Items should be stripped of surrounding whitespace
        for item in result:
            self.assertEqual(item, item.strip(), "Returned model strings must be stripped")

    def test_empty_stdout_yields_empty_list(self):
        with patch("director.opencode.subprocess.run", return_value=_fake_run("")):
            result = _runtime().discover_models()
        self.assertEqual(result, [])

    def test_stdout_with_only_non_slash_lines_yields_empty_list(self):
        stdout = "no models here\njust plain text\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, [])

    def test_mixed_valid_and_invalid_lines(self):
        stdout = "Loaded providers\nanthropic/claude-opus-4-8\n\nopenai/gpt-4o\nLoading complete\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["opencode/anthropic/claude-opus-4-8", "opencode/openai/gpt-4o"])


# --------------------------------------------------------------------------- #
# 5. subprocess.run call arguments
# --------------------------------------------------------------------------- #


class TestDiscoverModelsSubprocessArgs(unittest.TestCase):
    def test_invokes_opencode_models_command(self):
        with patch("director.opencode.subprocess.run", return_value=_fake_run("")) as mock_run:
            _runtime().discover_models()
        mock_run.assert_called_once()
        args = mock_run.call_args
        cmd = args[0][0] if args[0] else args[1].get("args", args[0])
        # First positional arg should be the command list
        self.assertEqual(cmd, ["opencode", "models"])

    def test_captures_stdout_as_text(self):
        with patch("director.opencode.subprocess.run", return_value=_fake_run("")) as mock_run:
            _runtime().discover_models()
        _, kwargs = mock_run.call_args
        self.assertTrue(
            kwargs.get("capture_output") or (kwargs.get("stdout") is not None),
            "discover_models must capture stdout",
        )
        self.assertTrue(
            kwargs.get("text") is True,
            "discover_models must pass text=True to subprocess.run",
        )

    def test_passes_timeout(self):
        with patch("director.opencode.subprocess.run", return_value=_fake_run("")) as mock_run:
            _runtime().discover_models()
        _, kwargs = mock_run.call_args
        self.assertIn("timeout", kwargs, "discover_models must pass a timeout to subprocess.run")
        self.assertIsInstance(kwargs["timeout"], (int, float))
        self.assertGreater(kwargs["timeout"], 0)


# --------------------------------------------------------------------------- #
# 6. Graceful degradation — MUST NEVER raise
# --------------------------------------------------------------------------- #


class TestDiscoverModelsGracefulDegradation(unittest.TestCase):
    def test_file_not_found_returns_empty_list(self):
        with patch(
            "director.opencode.subprocess.run", side_effect=FileNotFoundError("opencode not found")
        ):
            result = _runtime().discover_models()
        self.assertEqual(result, [], "FileNotFoundError must yield []")

    def test_called_process_error_returns_empty_list(self):
        exc = subprocess.CalledProcessError(1, ["opencode", "models"])
        with patch("director.opencode.subprocess.run", side_effect=exc):
            result = _runtime().discover_models()
        self.assertEqual(result, [], "CalledProcessError must yield []")

    def test_timeout_expired_returns_empty_list(self):
        exc = subprocess.TimeoutExpired(["opencode", "models"], 10)
        with patch("director.opencode.subprocess.run", side_effect=exc):
            result = _runtime().discover_models()
        self.assertEqual(result, [], "TimeoutExpired must yield []")

    def test_arbitrary_exception_returns_empty_list(self):
        with patch("director.opencode.subprocess.run", side_effect=RuntimeError("unexpected")):
            result = _runtime().discover_models()
        self.assertEqual(result, [], "Any arbitrary Exception must yield []")

    def test_os_error_returns_empty_list(self):
        with patch("director.opencode.subprocess.run", side_effect=OSError("permission denied")):
            result = _runtime().discover_models()
        self.assertEqual(result, [], "OSError must yield []")

    def test_method_does_not_raise_on_file_not_found(self):
        with patch("director.opencode.subprocess.run", side_effect=FileNotFoundError("missing")):
            try:
                _runtime().discover_models()
            except Exception as exc:
                self.fail(f"discover_models raised {type(exc).__name__}: {exc}")

    def test_method_does_not_raise_on_timeout(self):
        exc = subprocess.TimeoutExpired(["opencode", "models"], 10)
        with patch("director.opencode.subprocess.run", side_effect=exc):
            try:
                _runtime().discover_models()
            except Exception as e:
                self.fail(f"discover_models raised {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 7. Existing interface must remain intact
# --------------------------------------------------------------------------- #


class TestExistingInterfaceUnchanged(unittest.TestCase):
    def test_run_agent_still_importable(self):
        self.assertTrue(callable(getattr(oc, "run_agent", None)))

    def test_provider_for_model_still_importable(self):
        from director.provider import provider_for_model

        self.assertTrue(callable(provider_for_model))

    def test_opencode_providers_allowlist_removed(self):
        self.assertFalse(hasattr(oc, "OPENCODE_PROVIDERS"))

    def test_run_result_still_importable(self):
        from director.opencode import RunResult

        self.assertTrue(callable(RunResult))

    def test_watch_it_fail_still_importable(self):
        from director.opencode import watch_it_fail

        self.assertTrue(callable(watch_it_fail))

    def test_opencode_runtime_run_method_still_present(self):
        self.assertTrue(callable(getattr(oc.OpenCodeProvider, "run", None)))

    def test_opencode_runtime_system_prompt_for_still_present(self):
        self.assertTrue(callable(getattr(oc.OpenCodeProvider, "system_prompt_for", None)))


# --------------------------------------------------------------------------- #
# 8. Self-containment — no import from director.init
# --------------------------------------------------------------------------- #


class TestNoImportFromDirectorInit(unittest.TestCase):
    """discover_models must be self-contained; must not import director.init."""

    def _init_imports_in_opencode(self):
        tree = ast.parse(_OPENCODE_SRC.read_text())
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("director.init"):
                    names = ", ".join(a.name for a in node.names)
                    found.append(f"line {node.lineno}: from {node.module} import {names}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("director.init"):
                        found.append(f"line {node.lineno}: import {alias.name}")
        return found

    def test_opencode_does_not_import_director_init(self):
        violations = self._init_imports_in_opencode()
        self.assertEqual(
            violations,
            [],
            "director/opencode.py must not import from director.init:\n" + "\n".join(violations),
        )


# --------------------------------------------------------------------------- #
# 9. _run_opencode delegates process-tree lifecycle to director.proc
#
#    Node `callsite-opencode`: _run_opencode must launch the child via
#    proc.popen_tree (own process group/session) and, on timeout, tear down the
#    whole tree via proc.kill_tree(handle) — instead of subprocess.Popen +
#    proc.kill()/proc.wait() which orphans grandchildren.
#
#    These tests do NOT import director.proc (a sibling module that may not
#    exist yet); they patch opencode's reference to the proc module so they hold
#    regardless of whether the implementer aliases it `proc_mod` or `proc`.
# --------------------------------------------------------------------------- #


class _FakeHandle:
    """Stand-in for the Popen handle returned by popen_tree / subprocess.Popen.

    .wait(timeout=...) raises wait_exc only when a timeout is supplied (the call
    that detects a hang); the bare cleanup wait() in the legacy path returns
    normally so red runs surface a clean assertion failure, not a crash.
    """

    def __init__(self, *, wait_result=0, wait_exc=None, pid=4321):
        self.pid = pid
        self.wait_result = wait_result
        self.wait_exc = wait_exc
        self.wait_calls = []
        self.kill_calls = 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if timeout is not None and self.wait_exc is not None:
            raise self.wait_exc
        return self.wait_result

    def kill(self):
        self.kill_calls += 1

    def poll(self):
        return self.wait_result


class _RecordingPopen:
    """Records calls to the (now-forbidden) subprocess.Popen path."""

    def __init__(self, handle):
        self.handle = handle
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.handle


class _FakeProc:
    """Stand-in for the director.proc module that opencode delegates to."""

    def __init__(self, handle):
        self.handle = handle
        self.popen_tree_calls = []
        self.kill_tree_calls = []

    def popen_tree(self, *args, **kwargs):
        self.popen_tree_calls.append((args, kwargs))
        return self.handle

    def kill_tree(self, handle):
        self.kill_tree_calls.append(handle)
        # Real kill_tree reaps the process; emulate so callers don't block.
        with contextlib.suppress(Exception):
            handle.wait()


def _invoke(
    handle, *, agent="builder", model="anthropic/claude-opus-4-8", message="do it", timeout=300
):
    """Drive _run_opencode with the proc module and subprocess.Popen patched.

    Returns (result, fake_proc, rec_popen, expected_cmd, cwd). Both fakes hand
    back the SAME handle so its identity is stable across the two code paths.
    """
    fake_proc = _FakeProc(handle)
    rec_popen = _RecordingPopen(handle)
    with tempfile.TemporaryDirectory() as tmp:
        cwd = pathlib.Path(tmp)
        log_path = cwd / "logs" / "agent.ndjson"
        expected_cmd = [
            "opencode",
            "run",
            "--dir",
            str(cwd),
            "--agent",
            agent,
            "--model",
            model,
            "--format",
            "json",
            "--print-logs",
            message,
        ]
        with (
            patch.object(oc.subprocess, "Popen", rec_popen),
            patch.object(oc, "proc_mod", fake_proc, create=True),
            patch.object(oc, "proc", fake_proc, create=True),
        ):
            result = oc._run_opencode(
                agent=agent,
                model=model,
                message=message,
                cwd=cwd,
                log_path=log_path,
                timeout=timeout,
            )
    return result, fake_proc, rec_popen, expected_cmd, cwd


class TestRunOpencodeUsesProcTree(unittest.TestCase):
    def test_delegates_to_popen_tree(self):
        result, fake_proc, _rec, _cmd, _cwd = _invoke(_FakeHandle(wait_result=0))
        self.assertEqual(
            len(fake_proc.popen_tree_calls),
            1,
            "_run_opencode must launch the child via director.proc.popen_tree",
        )

    def test_popen_tree_receives_cmd_cwd_env_streams(self):
        result, fake_proc, _rec, expected_cmd, cwd = _invoke(_FakeHandle(wait_result=0))
        self.assertEqual(len(fake_proc.popen_tree_calls), 1)
        args, kwargs = fake_proc.popen_tree_calls[0]
        self.assertEqual(args[0], expected_cmd, "cmd must be passed positionally, unchanged")
        self.assertEqual(kwargs.get("cwd"), str(cwd), "same cwd as before must be forwarded")
        self.assertIs(kwargs.get("env"), oc._CLEAN_ENV, "_CLEAN_ENV must be forwarded as env")
        self.assertIsNotNone(kwargs.get("stdout"), "stdout handle must be forwarded")
        self.assertIsNotNone(kwargs.get("stderr"), "stderr handle must be forwarded")
        self.assertIsNot(
            kwargs.get("stdout"),
            kwargs.get("stderr"),
            "stdout and stderr must be distinct handles (separate .stderr file)",
        )

    def test_does_not_use_subprocess_popen(self):
        result, _fake, rec_popen, _cmd, _cwd = _invoke(_FakeHandle(wait_result=0))
        self.assertEqual(
            rec_popen.calls, [], "_run_opencode must not call subprocess.Popen directly"
        )


class TestRunOpencodeReturnShape(unittest.TestCase):
    def test_returns_runresult(self):
        result, *_ = _invoke(_FakeHandle(wait_result=0))
        self.assertIsInstance(result, oc.RunResult)

    def test_success_returncode_propagated(self):
        result, fake_proc, *_ = _invoke(_FakeHandle(wait_result=0))
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(fake_proc.kill_tree_calls, [], "no kill on the happy path")
        self.assertEqual(len(fake_proc.popen_tree_calls), 1)

    def test_nonzero_returncode_propagated(self):
        result, *_ = _invoke(_FakeHandle(wait_result=7))
        self.assertEqual(result.returncode, 7)
        self.assertFalse(result.timed_out)


class TestRunOpencodeTimeoutKillsTree(unittest.TestCase):
    def _timeout_handle(self):
        return _FakeHandle(
            wait_result=-9,
            wait_exc=subprocess.TimeoutExpired(["opencode"], 1),
        )

    def test_timeout_calls_kill_tree_with_handle(self):
        handle = self._timeout_handle()
        result, fake_proc, *_ = _invoke(handle, timeout=1)
        self.assertEqual(
            fake_proc.kill_tree_calls,
            [handle],
            "on TimeoutExpired, _run_opencode must call proc.kill_tree(handle)",
        )

    def test_timeout_return_shape_preserved(self):
        result, _fake, *_ = _invoke(self._timeout_handle(), timeout=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)

    def test_timeout_does_not_use_subprocess_popen(self):
        result, fake_proc, rec_popen, _cmd, _cwd = _invoke(self._timeout_handle(), timeout=1)
        self.assertEqual(rec_popen.calls, [], "timeout path must not touch subprocess.Popen")
        self.assertEqual(len(fake_proc.popen_tree_calls), 1)


class TestRunOpencodeSignatureUnchanged(unittest.TestCase):
    def test_keyword_only_signature_intact(self):
        sig = inspect.signature(oc._run_opencode)
        self.assertEqual(
            list(sig.parameters),
            ["agent", "model", "message", "cwd", "log_path", "timeout"],
        )
        for p in sig.parameters.values():
            self.assertEqual(
                p.kind,
                inspect.Parameter.KEYWORD_ONLY,
                f"parameter {p.name!r} must stay keyword-only",
            )


class TestRunOpencodeSourceUsesProc(unittest.TestCase):
    """Source-level guarantees (do not require director.proc to be importable)."""

    def setUp(self):
        self.tree = ast.parse(_OPENCODE_SRC.read_text())

    def _run_opencode_node(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_run_opencode":
                return node
        self.fail("_run_opencode function not found in opencode.py")

    def test_imports_proc_from_director(self):
        found = False
        for node in ast.walk(self.tree):
            if (
                (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "director"
                    and any(a.name == "proc" for a in node.names)
                )
                or isinstance(node, ast.Import)
                and any(a.name == "director.proc" for a in node.names)
            ):
                found = True
        self.assertTrue(
            found,
            "opencode.py must import proc from director "
            "(e.g. 'from director import proc as proc_mod')",
        )

    def test_run_opencode_calls_popen_tree(self):
        attrs = [
            n.attr for n in ast.walk(self._run_opencode_node()) if isinstance(n, ast.Attribute)
        ]
        self.assertIn("popen_tree", attrs, "_run_opencode must call proc.popen_tree")

    def test_run_opencode_calls_kill_tree(self):
        attrs = [
            n.attr for n in ast.walk(self._run_opencode_node()) if isinstance(n, ast.Attribute)
        ]
        self.assertIn("kill_tree", attrs, "_run_opencode must call proc.kill_tree on timeout")

    def test_run_opencode_no_subprocess_popen(self):
        for n in ast.walk(self._run_opencode_node()):
            if isinstance(n, ast.Attribute) and n.attr == "Popen":
                self.fail("_run_opencode must not call subprocess.Popen directly")


if __name__ == "__main__":
    unittest.main()
