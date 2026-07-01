"""Acceptance tests for ClaudeCodeProvider adapter in director.claudecode.

Tests cover:
1. Import redirect (AST): no 'from director.opencode import' remains in claudecode.py
2. ClaudeCodeProvider class: attributes, Provider protocol conformance
3. Model stripping: only the first path segment is removed from the model string
4. Module-global delegation: monkeypatching cc.run_claude / cc.system_prompt_for is honored
5. Registration: importing director.claudecode registers ClaudeCodeProvider in director.provider

Run: python3 -m unittest discover -s tests -p test_claudecode_runtime.py -q
"""

import ast
import importlib
import inspect
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# RunResult is needed only for constructing monkeypatch return values.
# Use director.provider if it exists (after the provider node is implemented),
# fall back to director.opencode (which always exists) otherwise.
try:
    from director.provider import RunResult as _RunResult
except ImportError:
    from director.opencode import RunResult as _RunResult

import director.claudecode as cc

_CLAUDECODE_SRC = pathlib.Path(__file__).resolve().parent.parent / "director" / "claudecode.py"


def setUpModule():
    """Refresh Claude Code adapter after provider registry isolation tests reload provider."""
    global _RunResult
    import director.provider as provider

    importlib.reload(provider)
    importlib.reload(cc)
    _RunResult = provider.RunResult


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _make_result(**kwargs):
    """Build a RunResult that works with both director.opencode and director.provider.

    director.opencode.RunResult requires text/tokens/cost_reported/n_steps as
    positional fields; director.provider.RunResult gives them safe defaults. We
    always supply them so the helper is forward- and backward-compatible.
    """
    base = {"returncode": 0, "text": "", "tokens": {}, "cost_reported": 0.0, "n_steps": 0}
    base.update(kwargs)
    return _RunResult(**base)


def _make_fake_run_claude(capture_dict):
    """Return a run_claude replacement that records its kwargs and returns rc=0."""

    def _fake(*, agent, model, message, cwd, log_path, timeout):
        capture_dict.update(agent=agent, model=model, message=message)
        return _make_result(returncode=0)

    return _fake


# --------------------------------------------------------------------------- #
# 1. Import redirect — AST check
# --------------------------------------------------------------------------- #


class TestImportRedirect(unittest.TestCase):
    """director/claudecode.py must have zero imports from director.opencode."""

    def _opencode_from_imports(self):
        tree = ast.parse(_CLAUDECODE_SRC.read_text())
        found = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("director.opencode")
            ):
                names = ", ".join(a.name for a in node.names)
                found.append(f"line {node.lineno}: from {node.module} import {names}")
        return found

    def _opencode_bare_imports(self):
        tree = ast.parse(_CLAUDECODE_SRC.read_text())
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("director.opencode"):
                        found.append(f"line {node.lineno}: import {alias.name}")
        return found

    def test_no_from_opencode_import_in_claudecode(self):
        violations = self._opencode_from_imports()
        self.assertEqual(
            violations,
            [],
            "director/claudecode.py still imports from director.opencode:\n"
            + "\n".join(violations),
        )

    def test_no_bare_import_director_opencode_in_claudecode(self):
        violations = self._opencode_bare_imports()
        self.assertEqual(
            violations,
            [],
            "director/claudecode.py still has bare 'import director.opencode':\n"
            + "\n".join(violations),
        )


# --------------------------------------------------------------------------- #
# 2. ClaudeCodeProvider class — existence and attributes
# --------------------------------------------------------------------------- #


class TestClaudeCodeProviderAttributes(unittest.TestCase):
    def test_class_exported_from_claudecode(self):
        self.assertTrue(
            hasattr(cc, "ClaudeCodeProvider"),
            "director.claudecode has no attribute 'ClaudeCodeProvider'",
        )

    def test_name_is_claude_code(self):
        self.assertEqual(cc.ClaudeCodeProvider.name, "claude-code")

    def test_provider_declares_name_only(self):
        self.assertFalse(hasattr(cc.ClaudeCodeProvider, "providers"))

    def test_has_callable_run_method(self):
        self.assertTrue(
            callable(getattr(cc.ClaudeCodeProvider, "run", None)),
            "ClaudeCodeProvider.run is not callable",
        )

    def test_has_callable_system_prompt_for_method(self):
        self.assertTrue(
            callable(getattr(cc.ClaudeCodeProvider, "system_prompt_for", None)),
            "ClaudeCodeProvider.system_prompt_for is not callable",
        )

    def test_run_has_all_required_kwargs(self):
        sig = inspect.signature(cc.ClaudeCodeProvider.run)
        params = set(sig.parameters.keys()) - {"self"}
        for kw in ("agent", "model", "message", "cwd", "log_path", "timeout"):
            self.assertIn(kw, params, f"ClaudeCodeProvider.run missing kwarg: {kw!r}")

    def test_system_prompt_for_has_agent_param(self):
        sig = inspect.signature(cc.ClaudeCodeProvider.system_prompt_for)
        self.assertIn("agent", sig.parameters)

    def test_instantiable_with_no_args(self):
        # ClaudeCodeProvider() must not require constructor arguments
        rt = cc.ClaudeCodeProvider()
        self.assertIsNotNone(rt)


# --------------------------------------------------------------------------- #
# 3. Model stripping — first path segment only
# --------------------------------------------------------------------------- #


class TestModelStripping(unittest.TestCase):
    """ClaudeCodeProvider.run() strips the first '/'-delimited segment of model."""

    def _captured_model(self, model_str):
        """Call ClaudeCodeProvider.run() with a fake run_claude; return captured model."""
        cap = {}
        orig = cc.run_claude
        cc.run_claude = _make_fake_run_claude(cap)
        try:
            with tempfile.TemporaryDirectory() as d:
                cc.ClaudeCodeProvider().run(
                    agent="planner",
                    model=model_str,
                    message="test",
                    cwd=d,
                    log_path=Path(d) / "out.json",
                    timeout=10,
                )
        finally:
            cc.run_claude = orig
        return cap.get("model")

    def test_simple_one_segment_stripped(self):
        # "claude-code/opus" → "opus"
        self.assertEqual(self._captured_model("claude-code/opus"), "opus")

    def test_nested_model_only_first_segment_stripped(self):
        # "claude-code/anthropic/claude-opus-4-8" → "anthropic/claude-opus-4-8"
        self.assertEqual(
            self._captured_model("claude-code/anthropic/claude-opus-4-8"),
            "anthropic/claude-opus-4-8",
        )

    def test_three_level_path_remaining_slashes_preserved(self):
        # "claude-code/a/b/c" → "a/b/c"
        self.assertEqual(self._captured_model("claude-code/a/b/c"), "a/b/c")

    def test_bedrock_style_model_remaining_slashes_preserved(self):
        # "claude-code/us.anthropic.claude-opus-4-7" → no remaining slash
        self.assertEqual(
            self._captured_model("claude-code/us.anthropic.claude-opus-4-7"),
            "us.anthropic.claude-opus-4-7",
        )


# --------------------------------------------------------------------------- #
# 4. Module-global delegation (monkeypatching)
# --------------------------------------------------------------------------- #


class TestModuleGlobalDelegation(unittest.TestCase):
    """ClaudeCodeProvider must reference run_claude and system_prompt_for by
    bare module-global name so that test-time monkeypatching is honored."""

    def test_run_calls_monkeypatched_run_claude(self):
        cap = {}
        orig = cc.run_claude
        cc.run_claude = _make_fake_run_claude(cap)
        try:
            with tempfile.TemporaryDirectory() as d:
                cc.ClaudeCodeProvider().run(
                    agent="executor",
                    model="claude-code/sonnet",
                    message="hello world",
                    cwd=d,
                    log_path=Path(d) / "out.json",
                    timeout=30,
                )
        finally:
            cc.run_claude = orig

        self.assertEqual(cap.get("agent"), "executor")
        self.assertEqual(cap.get("message"), "hello world")
        # model has been stripped
        self.assertEqual(cap.get("model"), "sonnet")

    def test_run_passes_cwd_log_path_timeout_through(self):
        cap = {}

        def _detailed_fake(*, agent, model, message, cwd, log_path, timeout):
            cap.update(cwd=cwd, log_path=log_path, timeout=timeout)
            return _make_result(returncode=0)

        orig = cc.run_claude
        cc.run_claude = _detailed_fake
        try:
            with tempfile.TemporaryDirectory() as d:
                lp = Path(d) / "out.json"
                cc.ClaudeCodeProvider().run(
                    agent="planner",
                    model="claude-code/opus",
                    message="m",
                    cwd=d,
                    log_path=lp,
                    timeout=99,
                )
        finally:
            cc.run_claude = orig

        self.assertEqual(cap.get("timeout"), 99)
        self.assertEqual(Path(str(cap.get("log_path"))), lp)

    def test_run_returns_whatever_run_claude_returns(self):
        sentinel = _make_result(returncode=42, text="sentinel")
        orig = cc.run_claude
        cc.run_claude = lambda **kw: sentinel  # noqa: E731
        try:
            with tempfile.TemporaryDirectory() as d:
                result = cc.ClaudeCodeProvider().run(
                    agent="planner",
                    model="claude-code/opus",
                    message="test",
                    cwd=d,
                    log_path=Path(d) / "out.json",
                    timeout=10,
                )
        finally:
            cc.run_claude = orig

        self.assertIs(result, sentinel)

    def test_system_prompt_for_honors_monkeypatched_function(self):
        orig = cc.system_prompt_for
        cc.system_prompt_for = lambda agent: f"PATCHED:{agent}"
        try:
            result = cc.ClaudeCodeProvider().system_prompt_for("planner")
        finally:
            cc.system_prompt_for = orig

        self.assertEqual(result, "PATCHED:planner")

    def test_system_prompt_for_passes_agent_unchanged(self):
        cap = {}
        orig = cc.system_prompt_for
        cc.system_prompt_for = lambda agent: cap.update(agent=agent) or "body"
        try:
            cc.ClaudeCodeProvider().system_prompt_for("executor")
        finally:
            cc.system_prompt_for = orig

        self.assertEqual(cap.get("agent"), "executor")

    def test_system_prompt_for_returns_delegated_result(self):
        orig = cc.system_prompt_for
        cc.system_prompt_for = lambda agent: None
        try:
            result = cc.ClaudeCodeProvider().system_prompt_for("whatever")
        finally:
            cc.system_prompt_for = orig

        self.assertIsNone(result)


# --------------------------------------------------------------------------- #
# 5. Registration at module load
# --------------------------------------------------------------------------- #


class TestRegistration(unittest.TestCase):
    """Importing director.claudecode must register ClaudeCodeProvider in
    director.provider._REGISTRY under the 'claude-code' provider key."""

    def _get_provider_module(self):
        try:
            import director.provider as rt

            return rt
        except ImportError:
            self.skipTest(
                "director.provider not yet implemented; "
                "registration tests deferred until the provider node is complete"
            )

    def test_registry_contains_claude_code_key_after_import(self):
        rt = self._get_provider_module()
        # The module-level import of director.claudecode (at top of this file)
        # should have triggered register(ClaudeCodeProvider()) already.
        import director.claudecode  # noqa: F401 — ensures side-effect ran

        self.assertIn(
            "claude-code",
            rt._REGISTRY,
            "director.provider._REGISTRY lacks 'claude-code' after importing director.claudecode",
        )

    def test_registered_value_is_claudecodert_instance(self):
        rt = self._get_provider_module()
        import director.claudecode as _cc  # noqa: F401

        entry = rt._REGISTRY.get("claude-code")
        self.assertIsNotNone(entry)
        self.assertIsInstance(
            entry,
            _cc.ClaudeCodeProvider,
            f"_REGISTRY['claude-code'] is {type(entry)!r}, expected ClaudeCodeProvider",
        )

    def test_registered_provider_name_matches(self):
        rt = self._get_provider_module()
        import director.claudecode  # noqa: F401

        entry = rt._REGISTRY.get("claude-code")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "claude-code")


if __name__ == "__main__":
    unittest.main()
