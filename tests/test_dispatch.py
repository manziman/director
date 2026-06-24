"""Acceptance tests for the `opencode-dispatch` node.

`director.opencode` gains per-role runtime selection WITHOUT changing the public
`run_agent` signature. Spec:

  (1) Module-level `_RUNTIME: dict[str, str] = {}` added after `_CLEAN_ENV`.
  (2) `set_runtime(mapping)` replaces the module global with a sanitised COPY:
      valid values are "opencode" and "claude-code"; anything else coerces to
      "opencode". `set_runtime({})` resets to empty.
  (3) The existing `run_agent` body is extracted verbatim into a private
      `_run_opencode(*, agent, model, message, cwd, log_path, timeout)`.
  (4) `run_agent` (same keyword-only signature) dispatches:
        backend = _RUNTIME.get(agent, "opencode")
        if backend == "claude-code":
            from director.claudecode import run_claude
            return run_claude(...)
        else:
            return _run_opencode(...)
      Empty `_RUNTIME` preserves identical behaviour to today.

These tests stub `_run_opencode` and a fake `director.claudecode.run_claude` so
no real CLI / network / model is invoked.

Run: python3 -m unittest discover -s tests -p test_dispatch.py -q
"""

from __future__ import annotations

import inspect
import os
import pathlib
import sys
import types
import unittest

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.opencode as oc  # noqa: E402
from director.opencode import RunResult, run_agent, watch_it_fail  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kwargs(**extra):
    """Build the full set of keyword-only args that run_agent requires."""
    base = {
        "agent": "executor",
        "model": "p/m1",
        "message": "do something",
        "cwd": ".",
        "log_path": "run.json",
        "timeout": 5,
    }
    base.update(extra)
    return base


def _result(text="ok") -> RunResult:
    return RunResult(
        returncode=0,
        text=text,
        tokens={"input": 0, "output": 0, "reasoning": 0, "total": 0},
        cost_reported=0.0,
        n_steps=0,
        tool_calls=[],
        tool_events=[],
        error=None,
        timed_out=False,
        log_path="run.json",
    )


class _FakeClaudeModule(types.ModuleType):
    """Stand-in for `director.claudecode` with a recording `run_claude`."""

    def __init__(self):
        super().__init__("director.claudecode")
        self.calls: list[dict] = []
        self.return_value = _result("claude-ran")

        def run_claude(*, agent, model, message, cwd, log_path, timeout):
            self.calls.append(
                {
                    "agent": agent,
                    "model": model,
                    "message": message,
                    "cwd": cwd,
                    "log_path": log_path,
                    "timeout": timeout,
                }
            )
            return self.return_value

        self.run_claude = run_claude


class _RuntimeResetMixin:
    """Reset `_RUNTIME` to {} before and after every test for isolation."""

    def setUp(self):
        oc.set_runtime({})

    def tearDown(self):
        oc.set_runtime({})


# ---------------------------------------------------------------------------
# 1. Module surface: new globals, setter, private runner
# ---------------------------------------------------------------------------


class TestModuleSurface(unittest.TestCase):
    """The new symbols must be importable and have the right shapes."""

    def test_runtime_global_exists(self):
        self.assertTrue(hasattr(oc, "_RUNTIME"), "_RUNTIME must exist on the module")

    def test_runtime_global_is_dict(self):
        self.assertIsInstance(oc._RUNTIME, dict)

    def test_runtime_default_is_empty(self):
        """The module-level default must be {} so existing behaviour is unchanged."""
        import importlib

        mod = importlib.import_module("director.opencode")
        # After a fresh import (or after set_runtime({})) it must be empty.
        # We call set_runtime({}) first to ensure a clean state.
        mod.set_runtime({})
        self.assertEqual(mod._RUNTIME, {})

    def test_set_runtime_is_callable(self):
        self.assertTrue(
            callable(getattr(oc, "set_runtime", None)),
            "set_runtime must be a callable on director.opencode",
        )

    def test_run_opencode_private_exists_and_callable(self):
        self.assertTrue(
            callable(getattr(oc, "_run_opencode", None)),
            "_run_opencode must be a callable on director.opencode",
        )

    def test_run_agent_signature_is_keyword_only(self):
        sig = inspect.signature(run_agent)
        for name, p in sig.parameters.items():
            self.assertEqual(
                p.kind,
                inspect.Parameter.KEYWORD_ONLY,
                f"run_agent param {name!r} must be keyword-only",
            )

    def test_run_agent_has_exact_params(self):
        sig = inspect.signature(run_agent)
        self.assertEqual(
            set(sig.parameters),
            {"agent", "model", "message", "cwd", "log_path", "timeout"},
        )

    def test_run_opencode_signature_is_keyword_only(self):
        sig = inspect.signature(oc._run_opencode)
        for name, p in sig.parameters.items():
            self.assertEqual(
                p.kind,
                inspect.Parameter.KEYWORD_ONLY,
                f"_run_opencode param {name!r} must be keyword-only",
            )

    def test_run_opencode_has_exact_params(self):
        sig = inspect.signature(oc._run_opencode)
        self.assertEqual(
            set(sig.parameters),
            {"agent", "model", "message", "cwd", "log_path", "timeout"},
        )

    def test_public_imports_still_work(self):
        """from director.opencode import run_agent, watch_it_fail must keep working."""
        self.assertTrue(callable(run_agent))
        self.assertTrue(callable(watch_it_fail))

    def test_set_runtime_importable(self):
        from director.opencode import set_runtime  # noqa: F401

        self.assertTrue(callable(set_runtime))

    def test_runresult_dataclass_unchanged(self):
        r = _result()
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "ok")
        self.assertIsNone(r.error)
        self.assertFalse(r.timed_out)

    def test_parse_still_callable(self):
        self.assertTrue(callable(oc._parse))

    def test_watch_it_fail_still_callable(self):
        self.assertTrue(callable(watch_it_fail))


# ---------------------------------------------------------------------------
# 2. set_runtime: storage and coercion semantics
# ---------------------------------------------------------------------------


class TestSetRuntime(_RuntimeResetMixin, unittest.TestCase):
    def test_empty_mapping_leaves_runtime_empty(self):
        oc.set_runtime({})
        self.assertEqual(oc._RUNTIME, {})

    def test_opencode_value_preserved(self):
        oc.set_runtime({"planner": "opencode"})
        self.assertEqual(oc._RUNTIME, {"planner": "opencode"})

    def test_claude_code_value_preserved(self):
        oc.set_runtime({"planner": "claude-code"})
        self.assertEqual(oc._RUNTIME, {"planner": "claude-code"})

    def test_bogus_value_coerced_to_opencode(self):
        oc.set_runtime({"planner": "bogus"})
        self.assertEqual(oc._RUNTIME, {"planner": "opencode"})

    def test_empty_string_coerced_to_opencode(self):
        oc.set_runtime({"executor": ""})
        self.assertEqual(oc._RUNTIME, {"executor": "opencode"})

    def test_mixed_values_coerced_selectively(self):
        oc.set_runtime(
            {
                "planner": "claude-code",
                "executor": "opencode",
                "reviewer": "weird",
                "explorer": "",
            }
        )
        self.assertEqual(
            oc._RUNTIME,
            {
                "planner": "claude-code",
                "executor": "opencode",
                "reviewer": "opencode",
                "explorer": "opencode",
            },
        )

    def test_stores_a_copy_not_the_same_object(self):
        """Mutating the caller's dict after set_runtime must not affect _RUNTIME."""
        mapping = {"planner": "claude-code"}
        oc.set_runtime(mapping)
        mapping["planner"] = "bogus"
        mapping["executor"] = "claude-code"
        self.assertEqual(oc._RUNTIME, {"planner": "claude-code"})

    def test_overwrites_previous_mapping_completely(self):
        oc.set_runtime({"planner": "claude-code"})
        oc.set_runtime({"executor": "claude-code"})
        # planner must be gone; only executor remains
        self.assertEqual(oc._RUNTIME, {"executor": "claude-code"})

    def test_reset_with_empty_clears_all(self):
        oc.set_runtime({"planner": "claude-code", "executor": "opencode"})
        oc.set_runtime({})
        self.assertEqual(oc._RUNTIME, {})

    def test_multiple_roles_all_stored(self):
        oc.set_runtime(
            {
                "planner": "claude-code",
                "executor": "opencode",
                "reviewer": "claude-code",
            }
        )
        self.assertEqual(oc._RUNTIME["planner"], "claude-code")
        self.assertEqual(oc._RUNTIME["executor"], "opencode")
        self.assertEqual(oc._RUNTIME["reviewer"], "claude-code")


# ---------------------------------------------------------------------------
# 3. run_agent dispatch: default (empty _RUNTIME) → opencode path
# ---------------------------------------------------------------------------


class TestDefaultDispatch(_RuntimeResetMixin, unittest.TestCase):
    """With empty _RUNTIME every role must route to _run_opencode."""

    def _patch_opencode_runner(self):
        """Replace _run_opencode with a recorder; return (calls_list, original)."""
        calls: list[dict] = []

        def fake(*, agent, model, message, cwd, log_path, timeout):
            calls.append(
                {
                    "agent": agent,
                    "model": model,
                    "message": message,
                    "cwd": cwd,
                    "log_path": log_path,
                    "timeout": timeout,
                }
            )
            return _result("opencode-ran")

        orig = oc._run_opencode
        oc._run_opencode = fake
        return calls, orig

    def test_empty_runtime_routes_to_opencode(self):
        calls, orig = self._patch_opencode_runner()
        try:
            r = run_agent(**_kwargs(agent="planner"))
        finally:
            oc._run_opencode = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["agent"], "planner")
        self.assertEqual(r.text, "opencode-ran")

    def test_unknown_role_routes_to_opencode(self):
        calls, orig = self._patch_opencode_runner()
        try:
            run_agent(**_kwargs(agent="not-a-known-role"))
        finally:
            oc._run_opencode = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["agent"], "not-a-known-role")

    def test_all_kwargs_forwarded_to_opencode(self):
        calls, orig = self._patch_opencode_runner()
        try:
            run_agent(
                agent="executor",
                model="openrouter/anthropic/x",
                message="hello world",
                cwd="/tmp/xyz",
                log_path="out.json",
                timeout=42,
            )
        finally:
            oc._run_opencode = orig
        c = calls[0]
        self.assertEqual(c["model"], "openrouter/anthropic/x")
        self.assertEqual(c["message"], "hello world")
        self.assertEqual(c["cwd"], "/tmp/xyz")
        self.assertEqual(c["log_path"], "out.json")
        self.assertEqual(c["timeout"], 42)

    def test_opencode_return_value_returned_unchanged(self):
        sentinel = _result("sentinel")
        orig = oc._run_opencode
        oc._run_opencode = lambda **kw: sentinel
        try:
            r = run_agent(**_kwargs())
        finally:
            oc._run_opencode = orig
        self.assertIs(r, sentinel)

    def test_opencode_result_is_runresult(self):
        calls, orig = self._patch_opencode_runner()
        try:
            r = run_agent(**_kwargs())
        finally:
            oc._run_opencode = orig
        self.assertIsInstance(r, RunResult)


# ---------------------------------------------------------------------------
# 4. run_agent dispatch: claude-code path (lazy import)
# ---------------------------------------------------------------------------


class TestClaudeDispatch(_RuntimeResetMixin, unittest.TestCase):
    """When a role is mapped to "claude-code", run_agent must delegate to
    director.claudecode.run_claude via a lazy import."""

    def _install_fake_claude(self):
        fake = _FakeClaudeModule()
        orig = sys.modules.get("director.claudecode")
        sys.modules["director.claudecode"] = fake
        return fake, orig

    def _restore_claude(self, fake, orig):
        if orig is None:
            sys.modules.pop("director.claudecode", None)
        else:
            sys.modules["director.claudecode"] = orig

    def test_claude_role_routes_to_run_claude(self):
        oc.set_runtime({"planner": "claude-code"})
        fake, orig = self._install_fake_claude()
        try:
            r = run_agent(**_kwargs(agent="planner"))
        finally:
            self._restore_claude(fake, orig)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["agent"], "planner")
        self.assertIs(r, fake.return_value)

    def test_claude_kwargs_forwarded_unchanged(self):
        oc.set_runtime({"planner": "claude-code"})
        fake, orig = self._install_fake_claude()
        try:
            run_agent(
                agent="planner",
                model="openrouter/anthropic/claude-opus-4.8",
                message="plan this",
                cwd="/tmp/work",
                log_path="logs/p.json",
                timeout=99,
            )
        finally:
            self._restore_claude(fake, orig)
        c = fake.calls[0]
        self.assertEqual(c["model"], "openrouter/anthropic/claude-opus-4.8")
        self.assertEqual(c["message"], "plan this")
        self.assertEqual(c["cwd"], "/tmp/work")
        self.assertEqual(c["log_path"], "logs/p.json")
        self.assertEqual(c["timeout"], 99)

    def test_other_role_still_opencode_when_one_role_is_claude(self):
        """Only the mapped role goes to claude; other roles still use opencode."""
        oc.set_runtime({"planner": "claude-code"})
        oc_calls: list[dict] = []
        orig_oc = oc._run_opencode

        def fake_oc(**kw):
            oc_calls.append(kw)
            return _result("opencode-ran")

        oc._run_opencode = fake_oc
        fake, orig = self._install_fake_claude()
        try:
            run_agent(**_kwargs(agent="executor"))
        finally:
            self._restore_claude(fake, orig)
            oc._run_opencode = orig_oc
        self.assertEqual(len(fake.calls), 0, "executor must NOT go through claude path")
        self.assertEqual(len(oc_calls), 1)
        self.assertEqual(oc_calls[0]["agent"], "executor")

    def test_bogus_value_coerces_back_to_opencode_at_dispatch(self):
        """set_runtime coerces "bogus" -> "opencode", so dispatch goes to opencode."""
        oc.set_runtime({"planner": "bogus"})
        oc_calls: list[dict] = []
        orig_oc = oc._run_opencode

        def fake_oc(**kw):
            oc_calls.append(kw)
            return _result("opencode-ran")

        oc._run_opencode = fake_oc
        fake, orig = self._install_fake_claude()
        try:
            run_agent(**_kwargs(agent="planner"))
        finally:
            self._restore_claude(fake, orig)
            oc._run_opencode = orig_oc
        self.assertEqual(
            len(fake.calls), 0, "bogus coerces to opencode; claude path must not be taken"
        )
        self.assertEqual(len(oc_calls), 1)
        self.assertEqual(oc_calls[0]["agent"], "planner")

    def test_claude_import_is_lazy_not_taken_for_opencode_role(self):
        """When a role routes to opencode, director.claudecode must never be imported."""
        oc.set_runtime({"planner": "claude-code"})
        sys.modules.pop("director.claudecode", None)
        orig_oc = oc._run_opencode
        oc._run_opencode = lambda **kw: _result("opencode-ran")
        try:
            run_agent(**_kwargs(agent="executor"))  # executor -> opencode, not planner
        finally:
            oc._run_opencode = orig_oc
        self.assertNotIn(
            "director.claudecode",
            sys.modules,
            "claudecode must not be imported when the dispatched role uses opencode",
        )

    def test_claude_result_is_runresult(self):
        oc.set_runtime({"executor": "claude-code"})
        fake, orig = self._install_fake_claude()
        try:
            r = run_agent(**_kwargs(agent="executor"))
        finally:
            self._restore_claude(fake, orig)
        self.assertIsInstance(r, RunResult)


# ---------------------------------------------------------------------------
# 5. Per-role independence: mixed mapping dispatches each role correctly
# ---------------------------------------------------------------------------


class TestPerRoleIndependence(_RuntimeResetMixin, unittest.TestCase):
    def test_each_role_dispatches_independently(self):
        oc.set_runtime(
            {
                "planner": "claude-code",
                "executor": "opencode",
                "reviewer": "claude-code",
            }
        )
        fake = _FakeClaudeModule()
        orig_cc = sys.modules.get("director.claudecode")
        sys.modules["director.claudecode"] = fake

        oc_calls: list[dict] = []
        orig_oc = oc._run_opencode
        oc._run_opencode = lambda **kw: (oc_calls.append(kw), _result("oc"))[1]
        try:
            run_agent(**_kwargs(agent="planner"))
            run_agent(**_kwargs(agent="executor"))
            run_agent(**_kwargs(agent="reviewer"))
        finally:
            if orig_cc is None:
                sys.modules.pop("director.claudecode", None)
            else:
                sys.modules["director.claudecode"] = orig_cc
            oc._run_opencode = orig_oc

        claude_agents = [c["agent"] for c in fake.calls]
        opencode_agents = [c["agent"] for c in oc_calls]
        self.assertEqual(claude_agents, ["planner", "reviewer"])
        self.assertEqual(opencode_agents, ["executor"])

    def test_unmapped_role_always_opencode(self):
        oc.set_runtime({"planner": "claude-code"})
        oc_calls: list[dict] = []
        orig_oc = oc._run_opencode
        oc._run_opencode = lambda **kw: (oc_calls.append(kw), _result("oc"))[1]
        fake = _FakeClaudeModule()
        orig_cc = sys.modules.get("director.claudecode")
        sys.modules["director.claudecode"] = fake
        try:
            run_agent(**_kwargs(agent="explorer"))
            run_agent(**_kwargs(agent="test_author"))
        finally:
            if orig_cc is None:
                sys.modules.pop("director.claudecode", None)
            else:
                sys.modules["director.claudecode"] = orig_cc
            oc._run_opencode = orig_oc
        self.assertEqual(len(fake.calls), 0)
        self.assertEqual(len(oc_calls), 2)

    def test_all_roles_claude_code(self):
        roles = ["planner", "executor", "reviewer", "explorer", "test_author"]
        oc.set_runtime(dict.fromkeys(roles, "claude-code"))
        fake = _FakeClaudeModule()
        orig_cc = sys.modules.get("director.claudecode")
        sys.modules["director.claudecode"] = fake
        orig_oc = oc._run_opencode
        oc._run_opencode = lambda **kw: (_ for _ in ()).throw(
            AssertionError("_run_opencode must not be called when all roles are claude-code")
        )
        try:
            for role in roles:
                run_agent(**_kwargs(agent=role))
        finally:
            if orig_cc is None:
                sys.modules.pop("director.claudecode", None)
            else:
                sys.modules["director.claudecode"] = orig_cc
            oc._run_opencode = orig_oc
        self.assertEqual(len(fake.calls), len(roles))
        self.assertEqual([c["agent"] for c in fake.calls], roles)


# ---------------------------------------------------------------------------
# 6. Regression: monkey-patch target `opencode.run_agent` still works
# ---------------------------------------------------------------------------


class TestMonkeyPatchTarget(_RuntimeResetMixin, unittest.TestCase):
    """The existing test suite stubs `opencode.run_agent` directly; that
    monkey-patch target must remain valid after the refactor."""

    def test_run_agent_attribute_is_writable_and_callable(self):
        sentinel = _result("patched")

        def fake(*, agent, model, message, cwd, log_path, timeout):
            return sentinel

        orig = oc.run_agent
        oc.run_agent = fake
        try:
            r = oc.run_agent(**_kwargs())
        finally:
            oc.run_agent = orig
        self.assertIs(r, sentinel)

    def test_run_agent_module_attribute_is_same_object_as_imported(self):
        self.assertIs(oc.run_agent, run_agent)


# ---------------------------------------------------------------------------
# 7. Invariants: _RUNTIME is always a dict after set_runtime
# ---------------------------------------------------------------------------


class TestRuntimeInvariants(_RuntimeResetMixin, unittest.TestCase):
    def test_runtime_is_dict_after_set_runtime_empty(self):
        oc.set_runtime({})
        self.assertIsInstance(oc._RUNTIME, dict)

    def test_runtime_is_dict_after_set_runtime_nonempty(self):
        oc.set_runtime({"planner": "claude-code"})
        self.assertIsInstance(oc._RUNTIME, dict)

    def test_runtime_values_are_only_valid_backends(self):
        oc.set_runtime(
            {
                "planner": "claude-code",
                "executor": "opencode",
                "reviewer": "anything-else",
            }
        )
        for role, backend in oc._RUNTIME.items():
            self.assertIn(
                backend,
                ("opencode", "claude-code"),
                f"role {role!r} has invalid backend {backend!r} after set_runtime",
            )

    def test_runtime_is_independent_copy(self):
        """_RUNTIME must be a new dict object, not the one passed in."""
        mapping = {"planner": "claude-code"}
        oc.set_runtime(mapping)
        self.assertIsNot(oc._RUNTIME, mapping)


if __name__ == "__main__":
    unittest.main(verbosity=2)
