"""Acceptance tests for OpenCodeRuntime.discover_models.

Covers:
  - Method existence and correct signature on OpenCodeRuntime
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
import inspect
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.opencode as oc

_OPENCODE_SRC = pathlib.Path(__file__).resolve().parent.parent / "director" / "opencode.py"

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
    return oc.OpenCodeRuntime()


# --------------------------------------------------------------------------- #
# 1. Method existence and signature
# --------------------------------------------------------------------------- #


class TestDiscoverModelsExists(unittest.TestCase):
    def test_opencode_runtime_has_discover_models(self):
        self.assertTrue(
            hasattr(oc.OpenCodeRuntime, "discover_models"),
            "OpenCodeRuntime must have a discover_models attribute",
        )

    def test_discover_models_is_callable(self):
        self.assertTrue(
            callable(getattr(oc.OpenCodeRuntime, "discover_models", None)),
            "OpenCodeRuntime.discover_models must be callable",
        )

    def test_discover_models_takes_only_self(self):
        sig = inspect.signature(oc.OpenCodeRuntime.discover_models)
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
        self.assertEqual(result, ["anthropic/claude-sonnet-4-6"])

    def test_multiple_model_lines_returned(self):
        stdout = "anthropic/claude-opus-4-8\nopenai/gpt-4o\nGoogle/gemini-2.5-pro\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(
            result,
            ["anthropic/claude-opus-4-8", "openai/gpt-4o", "Google/gemini-2.5-pro"],
        )

    def test_order_is_preserved(self):
        models = [f"provider{i}/model{i}" for i in range(5)]
        stdout = "\n".join(models) + "\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, models)

    def test_deeply_nested_model_id_kept(self):
        # e.g. "google-vertex/us-central1/gemini-2.0-flash"
        stdout = "google-vertex/us-central1/gemini-2.0-flash\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["google-vertex/us-central1/gemini-2.0-flash"])


# --------------------------------------------------------------------------- #
# 4. Filtering: empty lines and lines without '/'
# --------------------------------------------------------------------------- #


class TestDiscoverModelsFiltering(unittest.TestCase):
    def test_empty_lines_dropped(self):
        stdout = "\nanthropic/claude-opus-4-8\n\n\nopenai/gpt-4o\n\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["anthropic/claude-opus-4-8", "openai/gpt-4o"])

    def test_lines_without_slash_dropped(self):
        stdout = "Available models:\nanthropic/claude-sonnet-4-6\nsome-bare-token\nopenai/gpt-4o\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertNotIn("Available models:", result)
        self.assertNotIn("some-bare-token", result)
        self.assertIn("anthropic/claude-sonnet-4-6", result)
        self.assertIn("openai/gpt-4o", result)

    def test_whitespace_only_lines_dropped(self):
        stdout = "anthropic/claude-opus-4-8\n   \n\t\nopenai/gpt-4o\n"
        with patch("director.opencode.subprocess.run", return_value=_fake_run(stdout)):
            result = _runtime().discover_models()
        self.assertEqual(result, ["anthropic/claude-opus-4-8", "openai/gpt-4o"])

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
        self.assertEqual(result, ["anthropic/claude-opus-4-8", "openai/gpt-4o"])


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

    def test_runtime_for_model_still_importable(self):
        from director.runtime import runtime_for_model

        self.assertTrue(callable(runtime_for_model))

    def test_opencode_providers_still_present(self):
        self.assertIsInstance(oc.OPENCODE_PROVIDERS, frozenset)
        self.assertIn("anthropic", oc.OPENCODE_PROVIDERS)

    def test_run_result_still_importable(self):
        from director.opencode import RunResult

        self.assertTrue(callable(RunResult))

    def test_watch_it_fail_still_importable(self):
        from director.opencode import watch_it_fail

        self.assertTrue(callable(watch_it_fail))

    def test_opencode_runtime_run_method_still_present(self):
        self.assertTrue(callable(getattr(oc.OpenCodeRuntime, "run", None)))

    def test_opencode_runtime_system_prompt_for_still_present(self):
        self.assertTrue(callable(getattr(oc.OpenCodeRuntime, "system_prompt_for", None)))


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


if __name__ == "__main__":
    unittest.main()
