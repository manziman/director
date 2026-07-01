"""Acceptance tests for CLAUDE_CODE_MODELS constant + ClaudeCodeProvider.discover_models.

Covers:
  - CLAUDE_CODE_MODELS exists as a module-level tuple with the three stable aliases
  - ClaudeCodeProvider.discover_models exists and has the right signature
  - Return value is exactly ["claude-code/opus", "claude-code/sonnet", "claude-code/haiku"]
  - Returns a list (not a tuple or other iterable)
  - No subprocess work — discover_models never touches the filesystem or shell
  - Always returns the fixed list regardless of claude binary presence
  - Existing ClaudeCodeProvider interface (run, system_prompt_for) is untouched
  - director.claudecode does NOT import from director.opencode

Run: python3 -m unittest tests.test_claudecode_discovery -v
"""

import ast
import importlib
import inspect
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.claudecode as cc

_CC_SRC = pathlib.Path(__file__).resolve().parent.parent / "director" / "claudecode.py"


def setUpModule():
    """Refresh Claude Code adapter after provider registry isolation tests reload provider."""
    import director.codex as codex
    import director.provider as provider

    importlib.reload(provider)
    importlib.reload(cc)
    importlib.reload(codex)


EXPECTED_MODELS = ["claude-code/opus", "claude-code/sonnet", "claude-code/haiku"]


# --------------------------------------------------------------------------- #
# 1. CLAUDE_CODE_MODELS constant
# --------------------------------------------------------------------------- #


class TestClaudeCodeModelsConstant(unittest.TestCase):
    def test_constant_exists(self):
        self.assertTrue(
            hasattr(cc, "CLAUDE_CODE_MODELS"),
            "director.claudecode must expose CLAUDE_CODE_MODELS at module level",
        )

    def test_constant_is_tuple(self):
        self.assertIsInstance(
            cc.CLAUDE_CODE_MODELS,
            tuple,
            "CLAUDE_CODE_MODELS must be a tuple",
        )

    def test_constant_contains_opus(self):
        self.assertIn("opus", cc.CLAUDE_CODE_MODELS)

    def test_constant_contains_sonnet(self):
        self.assertIn("sonnet", cc.CLAUDE_CODE_MODELS)

    def test_constant_contains_haiku(self):
        self.assertIn("haiku", cc.CLAUDE_CODE_MODELS)

    def test_constant_has_exactly_three_entries(self):
        self.assertEqual(
            len(cc.CLAUDE_CODE_MODELS),
            3,
            f"CLAUDE_CODE_MODELS must have exactly 3 entries, got {len(cc.CLAUDE_CODE_MODELS)}",
        )

    def test_constant_entries_are_strings(self):
        for entry in cc.CLAUDE_CODE_MODELS:
            self.assertIsInstance(entry, str, f"Every entry must be str, got {type(entry)}")


# --------------------------------------------------------------------------- #
# 2. ClaudeCodeProvider.discover_models — existence and signature
# --------------------------------------------------------------------------- #


class TestDiscoverModelsExists(unittest.TestCase):
    def test_claudecode_runtime_has_discover_models(self):
        self.assertTrue(
            hasattr(cc.ClaudeCodeProvider, "discover_models"),
            "ClaudeCodeProvider must have a discover_models attribute",
        )

    def test_discover_models_is_callable(self):
        self.assertTrue(
            callable(getattr(cc.ClaudeCodeProvider, "discover_models", None)),
            "ClaudeCodeProvider.discover_models must be callable",
        )

    def test_discover_models_takes_only_self(self):
        sig = inspect.signature(cc.ClaudeCodeProvider.discover_models)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params,
            ["self"],
            "discover_models must accept only 'self' — no extra parameters",
        )


# --------------------------------------------------------------------------- #
# 3. Return value — exact content and type
# --------------------------------------------------------------------------- #


class TestDiscoverModelsReturnValue(unittest.TestCase):
    def setUp(self):
        self.provider = cc.ClaudeCodeProvider()

    def test_returns_list(self):
        result = self.provider.discover_models()
        self.assertIsInstance(result, list, "discover_models must return a list")

    def test_returns_exactly_three_items(self):
        result = self.provider.discover_models()
        self.assertEqual(len(result), 3, f"discover_models must return 3 items, got {len(result)}")

    def test_returns_expected_list(self):
        result = self.provider.discover_models()
        self.assertEqual(result, EXPECTED_MODELS)

    def test_opus_entry_present(self):
        result = self.provider.discover_models()
        self.assertIn("claude-code/opus", result)

    def test_sonnet_entry_present(self):
        result = self.provider.discover_models()
        self.assertIn("claude-code/sonnet", result)

    def test_haiku_entry_present(self):
        result = self.provider.discover_models()
        self.assertIn("claude-code/haiku", result)

    def test_all_entries_are_strings(self):
        result = self.provider.discover_models()
        for item in result:
            self.assertIsInstance(item, str, f"All returned items must be str, got {type(item)}")

    def test_all_entries_have_claude_code_prefix(self):
        result = self.provider.discover_models()
        for item in result:
            self.assertTrue(
                item.startswith("claude-code/"),
                f"Each entry must start with 'claude-code/', got {item!r}",
            )

    def test_order_is_opus_sonnet_haiku(self):
        result = self.provider.discover_models()
        self.assertEqual(result[0], "claude-code/opus")
        self.assertEqual(result[1], "claude-code/sonnet")
        self.assertEqual(result[2], "claude-code/haiku")


# --------------------------------------------------------------------------- #
# 4. No subprocess — static/pure method
# --------------------------------------------------------------------------- #


class TestDiscoverModelsNoSubprocess(unittest.TestCase):
    def test_does_not_invoke_subprocess(self):
        import subprocess as _subprocess
        from unittest.mock import patch

        provider = cc.ClaudeCodeProvider()
        with (
            patch.object(
                _subprocess, "run", side_effect=AssertionError("subprocess.run called")
            ) as mock_run,
            patch.object(
                _subprocess, "Popen", side_effect=AssertionError("subprocess.Popen called")
            ) as mock_popen,
        ):
            try:
                provider.discover_models()
            except AssertionError as exc:
                self.fail(f"discover_models called subprocess: {exc}")
        mock_run.assert_not_called()
        mock_popen.assert_not_called()

    def test_does_not_raise_when_claude_binary_absent(self):
        import subprocess as _subprocess
        from unittest.mock import patch

        provider = cc.ClaudeCodeProvider()
        with (
            patch.object(_subprocess, "run", side_effect=FileNotFoundError("claude not found")),
            patch.object(_subprocess, "Popen", side_effect=FileNotFoundError("claude not found")),
        ):
            try:
                provider.discover_models()
            except Exception as exc:
                self.fail(f"discover_models raised {type(exc).__name__}: {exc}")

    def test_result_unchanged_even_if_subprocess_would_fail(self):
        import subprocess as _subprocess
        from unittest.mock import patch

        provider = cc.ClaudeCodeProvider()
        with (
            patch.object(_subprocess, "run", side_effect=FileNotFoundError("claude not found")),
            patch.object(_subprocess, "Popen", side_effect=FileNotFoundError("claude not found")),
        ):
            result = provider.discover_models()
        self.assertEqual(result, EXPECTED_MODELS)

    def test_idempotent_multiple_calls(self):
        provider = cc.ClaudeCodeProvider()
        first = provider.discover_models()
        second = provider.discover_models()
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# 5. Existing ClaudeCodeProvider interface unchanged
# --------------------------------------------------------------------------- #


class TestExistingInterfaceUnchanged(unittest.TestCase):
    def test_run_method_still_present(self):
        self.assertTrue(
            callable(getattr(cc.ClaudeCodeProvider, "run", None)),
            "ClaudeCodeProvider.run must still be callable",
        )

    def test_system_prompt_for_still_present(self):
        self.assertTrue(
            callable(getattr(cc.ClaudeCodeProvider, "system_prompt_for", None)),
            "ClaudeCodeProvider.system_prompt_for must still be callable",
        )

    def test_provider_name_still_claude_code(self):
        provider = cc.ClaudeCodeProvider()
        self.assertEqual(provider.name, "claude-code")

    def test_provider_declares_name_only(self):
        provider = cc.ClaudeCodeProvider()
        self.assertFalse(hasattr(provider, "providers"))

    def test_run_claude_still_importable(self):
        from director.claudecode import run_claude

        self.assertTrue(callable(run_claude))

    def test_system_prompt_for_func_still_importable(self):
        from director.claudecode import system_prompt_for

        self.assertTrue(callable(system_prompt_for))


# --------------------------------------------------------------------------- #
# 6. No import from director.opencode (self-containment)
# --------------------------------------------------------------------------- #


class TestNoImportFromDirectorOpencode(unittest.TestCase):
    """discover_models and CLAUDE_CODE_MODELS must not require director.opencode."""

    def _opencode_imports_in_claudecode(self):
        tree = ast.parse(_CC_SRC.read_text())
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("director.opencode"):
                    names = ", ".join(a.name for a in node.names)
                    found.append(f"line {node.lineno}: from {node.module} import {names}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("director.opencode"):
                        found.append(f"line {node.lineno}: import {alias.name}")
        return found

    def test_claudecode_does_not_import_director_opencode(self):
        violations = self._opencode_imports_in_claudecode()
        self.assertEqual(
            violations,
            [],
            "director/claudecode.py must not import from director.opencode:\n"
            + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
