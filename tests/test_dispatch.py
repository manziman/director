"""Registry-based dispatch tests for director.opencode.

After the opencode-dispatch refactor, run_agent routes via the runtime registry
instead of a hardcoded prefix check. Tests verify:

1. Module surface: OPENCODE_PROVIDERS, no CLAUDE_PREFIX, re-exports from runtime
2. RunResult / _CLEAN_ENV identity: oc.* is rt.* (same objects)
3. OPENCODE_PROVIDERS content (required providers, must not contain claude-code)
4. OpenCodeRuntime class: name, providers, protocol conformance
5. Registry state after import (both runtimes registered)
6. Dispatch routing: claude-code/* → ClaudeCodeRuntime, opencode providers → OpenCodeRuntime
7. Unknown provider → error RunResult (never raises, .ok is False, error names provider)
8. No-elif extensibility: register a dummy runtime, run_agent routes to it
9. Registration collision raises ValueError
10. Monkeypatchability of run_agent, _run_opencode, run_claude

Run: python3 -m unittest discover -s tests -p test_dispatch.py -q
"""

from __future__ import annotations

import inspect
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import contextlib

import director.claudecode as cc  # noqa: E402
import director.opencode as oc  # noqa: E402
import director.runtime as rt  # noqa: E402
from director.opencode import (  # noqa: E402
    OPENCODE_PROVIDERS,
    RunResult,
    _run_opencode,
    run_agent,
    watch_it_fail,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _kwargs(**extra):
    """Build a minimal valid kwargs dict for run_agent."""
    base = {
        "agent": "executor",
        "model": "lmstudio/qwen3.6-27b",
        "message": "do something",
        "cwd": ".",
        "log_path": "run.json",
        "timeout": 5,
    }
    base.update(extra)
    return base


def _ok(text: str = "ok") -> RunResult:
    return RunResult(returncode=0, text=text, timed_out=False)


# --------------------------------------------------------------------------- #
# Mixins
# --------------------------------------------------------------------------- #


class _RegistryGuard:
    """setUp/tearDown that snapshots and restores rt._REGISTRY.

    Mix into test classes that call rt.register() so mutations don't leak
    across tests.
    """

    def setUp(self):
        super().setUp()
        self._reg_snap = dict(rt._REGISTRY)

    def tearDown(self):
        rt._REGISTRY.clear()
        rt._REGISTRY.update(self._reg_snap)
        super().tearDown()


class _DispatchMixin:
    """Monkeypatch _run_opencode and run_claude; record calls; restore after."""

    def setUp(self):
        super().setUp()
        self.oc_calls = []
        self.claude_calls = []
        self._orig_run_oc = oc._run_opencode
        self._orig_run_claude = cc.run_claude
        _oc = self.oc_calls
        _cc = self.claude_calls
        oc._run_opencode = lambda **kw: (_oc.append(kw), _ok("oc"))[1]
        cc.run_claude = lambda **kw: (_cc.append(kw), _ok("claude"))[1]

    def tearDown(self):
        oc._run_opencode = self._orig_run_oc
        cc.run_claude = self._orig_run_claude
        super().tearDown()


# --------------------------------------------------------------------------- #
# 1. Module surface
# --------------------------------------------------------------------------- #


class TestModuleSurface(unittest.TestCase):
    def test_claude_prefix_is_gone(self):
        """CLAUDE_PREFIX was removed; registry dispatch replaced prefix routing."""
        self.assertFalse(
            hasattr(oc, "CLAUDE_PREFIX"),
            "oc.CLAUDE_PREFIX still exists — prefix dispatch was not removed",
        )

    def test_opencode_providers_exists(self):
        self.assertTrue(hasattr(oc, "OPENCODE_PROVIDERS"))

    def test_opencode_providers_is_frozenset(self):
        self.assertIsInstance(OPENCODE_PROVIDERS, frozenset)

    def test_run_agent_present_and_callable(self):
        self.assertTrue(callable(run_agent))

    def test_run_opencode_present_and_callable(self):
        self.assertTrue(callable(_run_opencode))

    def test_watch_it_fail_present_and_callable(self):
        self.assertTrue(callable(watch_it_fail))

    def test_run_agent_exact_signature(self):
        sig = inspect.signature(run_agent)
        self.assertEqual(
            set(sig.parameters),
            {"agent", "model", "message", "cwd", "log_path", "timeout"},
        )

    def test_opencode_runtime_class_exported(self):
        self.assertTrue(
            hasattr(oc, "OpenCodeRuntime"),
            "director.opencode has no attribute 'OpenCodeRuntime'",
        )

    def test_opencode_runtime_name(self):
        self.assertEqual(oc.OpenCodeRuntime.name, "opencode")

    def test_opencode_runtime_providers_is_frozenset(self):
        self.assertIsInstance(oc.OpenCodeRuntime.providers, frozenset)

    def test_opencode_runtime_providers_equals_opencode_providers(self):
        self.assertEqual(oc.OpenCodeRuntime.providers, OPENCODE_PROVIDERS)

    def test_opencode_runtime_has_run_method(self):
        self.assertTrue(callable(getattr(oc.OpenCodeRuntime, "run", None)))

    def test_opencode_runtime_has_system_prompt_for(self):
        self.assertTrue(callable(getattr(oc.OpenCodeRuntime, "system_prompt_for", None)))

    def test_opencode_runtime_system_prompt_for_returns_none(self):
        inst = oc.OpenCodeRuntime()
        self.assertIsNone(inst.system_prompt_for("planner"))

    def test_opencode_runtime_run_signature(self):
        sig = inspect.signature(oc.OpenCodeRuntime.run)
        params = set(sig.parameters.keys()) - {"self"}
        for kw in ("agent", "model", "message", "cwd", "log_path", "timeout"):
            self.assertIn(kw, params, f"OpenCodeRuntime.run missing kwarg: {kw!r}")


# --------------------------------------------------------------------------- #
# 2. Re-exports — oc.RunResult is rt.RunResult (same class object)
# --------------------------------------------------------------------------- #


class TestReExports(unittest.TestCase):
    def test_run_result_is_same_class_object(self):
        """oc.RunResult must be the SAME class object as rt.RunResult."""
        self.assertIs(oc.RunResult, rt.RunResult)

    def test_clean_env_is_same_dict_object(self):
        """oc._CLEAN_ENV must be the SAME dict object as rt._CLEAN_ENV."""
        self.assertIs(oc._CLEAN_ENV, rt._CLEAN_ENV)

    def test_from_opencode_import_run_result_is_rt_run_result(self):
        from director.opencode import RunResult as oc_RR

        self.assertIs(oc_RR, rt.RunResult)

    def test_from_opencode_import_clean_env_is_rt_clean_env(self):
        from director.opencode import _CLEAN_ENV as oc_ce

        self.assertIs(oc_ce, rt._CLEAN_ENV)

    def test_run_result_constructed_with_returncode_only(self):
        """Minimal construction via re-exported class must work."""
        r = RunResult(returncode=0)
        self.assertEqual(r.returncode, 0)


# --------------------------------------------------------------------------- #
# 3. OPENCODE_PROVIDERS content
# --------------------------------------------------------------------------- #

_REQUIRED_PROVIDERS = [
    "anthropic",
    "openai",
    "google",
    "google-vertex",
    "amazon-bedrock",
    "azure",
    "openrouter",
    "lmstudio",
    "deepseek",
    "groq",
    "mistral",
    "togetherai",
    "fireworks",
    "xai",
    "perplexity",
    "cohere",
    "cerebras",
    "deepinfra",
    "huggingface",
    "ollama",
    "github-copilot",
    "github-models",
    "requesty",
    "v0",
    "inception",
    "morph",
    "upstage",
    "baseten",
    "nebius",
    "sambanova",
    "alibaba",
    "openai-compatible",
    "llama",
]


class TestOpenCodeProviders(unittest.TestCase):
    def test_does_not_contain_claude_code(self):
        """'claude-code' belongs to ClaudeCodeRuntime, not OpenCodeRuntime."""
        self.assertNotIn("claude-code", OPENCODE_PROVIDERS)

    def test_contains_all_required_providers(self):
        missing = [p for p in _REQUIRED_PROVIDERS if p not in OPENCODE_PROVIDERS]
        self.assertEqual(
            missing,
            [],
            f"OPENCODE_PROVIDERS is missing required providers: {missing}",
        )

    def test_contains_anthropic(self):
        self.assertIn("anthropic", OPENCODE_PROVIDERS)

    def test_contains_openai(self):
        self.assertIn("openai", OPENCODE_PROVIDERS)

    def test_contains_lmstudio(self):
        self.assertIn("lmstudio", OPENCODE_PROVIDERS)

    def test_contains_amazon_bedrock(self):
        self.assertIn("amazon-bedrock", OPENCODE_PROVIDERS)

    def test_contains_openrouter(self):
        self.assertIn("openrouter", OPENCODE_PROVIDERS)

    def test_contains_google(self):
        self.assertIn("google", OPENCODE_PROVIDERS)

    def test_contains_ollama(self):
        self.assertIn("ollama", OPENCODE_PROVIDERS)


# --------------------------------------------------------------------------- #
# 4. Registry state after import
# --------------------------------------------------------------------------- #


class TestRegistryOnImport(unittest.TestCase):
    def test_opencode_runtime_registered_for_anthropic(self):
        """Importing director.opencode registers OpenCodeRuntime for 'anthropic'."""
        entry = rt._REGISTRY.get("anthropic")
        self.assertIsNotNone(entry, "'anthropic' missing from registry after oc import")
        self.assertIsInstance(entry, oc.OpenCodeRuntime)

    def test_opencode_runtime_registered_for_lmstudio(self):
        entry = rt._REGISTRY.get("lmstudio")
        self.assertIsNotNone(entry, "'lmstudio' missing from registry after oc import")
        self.assertIsInstance(entry, oc.OpenCodeRuntime)

    def test_opencode_runtime_registered_for_amazon_bedrock(self):
        entry = rt._REGISTRY.get("amazon-bedrock")
        self.assertIsNotNone(entry)
        self.assertIsInstance(entry, oc.OpenCodeRuntime)

    def test_claude_code_runtime_registered_after_oc_import(self):
        """Importing director.opencode triggers import director.claudecode."""
        entry = rt._REGISTRY.get("claude-code")
        self.assertIsNotNone(entry, "'claude-code' missing from registry after oc import")
        self.assertIsInstance(entry, cc.ClaudeCodeRuntime)

    def test_resolve_lmstudio_returns_opencode_runtime(self):
        entry = rt.resolve("lmstudio")
        self.assertIsNotNone(entry)
        self.assertIsInstance(entry, oc.OpenCodeRuntime)

    def test_resolve_claude_code_returns_claudecode_runtime(self):
        entry = rt.resolve("claude-code")
        self.assertIsNotNone(entry)
        self.assertIsInstance(entry, cc.ClaudeCodeRuntime)

    def test_runtime_for_model_resolves_lmstudio_model(self):
        from director.opencode import runtime_for_model

        resolved = runtime_for_model("lmstudio/qwen3.6-27b")
        self.assertIsNotNone(resolved)
        self.assertIsInstance(resolved, oc.OpenCodeRuntime)

    def test_runtime_for_model_resolves_claude_code_model(self):
        from director.opencode import runtime_for_model

        resolved = runtime_for_model("claude-code/opus")
        self.assertIsNotNone(resolved)
        self.assertIsInstance(resolved, cc.ClaudeCodeRuntime)

    def test_resolve_and_runtime_for_model_agree_on_lmstudio(self):
        from director.opencode import runtime_for_model

        self.assertIs(rt.resolve("lmstudio"), runtime_for_model("lmstudio/qwen"))

    def test_resolve_unknown_returns_none(self):
        self.assertIsNone(rt.resolve("xyz-totally-unknown"))


# --------------------------------------------------------------------------- #
# 5. Dispatch routing
# --------------------------------------------------------------------------- #


class TestDispatchRouting(_DispatchMixin, unittest.TestCase):
    def test_claude_code_simple_routes_to_run_claude_model_stripped(self):
        r = run_agent(**_kwargs(model="claude-code/opus"))
        self.assertEqual(r.text, "claude")
        self.assertEqual(len(self.claude_calls), 1)
        self.assertEqual(len(self.oc_calls), 0)
        self.assertEqual(self.claude_calls[0]["model"], "opus")

    def test_claude_code_nested_model_only_first_segment_stripped(self):
        run_agent(**_kwargs(model="claude-code/anthropic/claude-opus-4-8"))
        self.assertEqual(len(self.claude_calls), 1)
        self.assertEqual(self.claude_calls[0]["model"], "anthropic/claude-opus-4-8")

    def test_claude_code_three_level_remaining_slashes_preserved(self):
        run_agent(**_kwargs(model="claude-code/a/b/c"))
        self.assertEqual(self.claude_calls[0]["model"], "a/b/c")

    def test_anthropic_routes_to_opencode_full_model_unchanged(self):
        run_agent(**_kwargs(model="anthropic/claude-opus-4-8"))
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(len(self.claude_calls), 0)
        self.assertEqual(self.oc_calls[0]["model"], "anthropic/claude-opus-4-8")

    def test_lmstudio_routes_to_opencode_full_model_unchanged(self):
        run_agent(**_kwargs(model="lmstudio/qwen3.6-27b-mtp"))
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(self.oc_calls[0]["model"], "lmstudio/qwen3.6-27b-mtp")

    def test_amazon_bedrock_routes_to_opencode_full_model_unchanged(self):
        run_agent(**_kwargs(model="amazon-bedrock/us.anthropic.claude-opus-4-7"))
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(len(self.claude_calls), 0)
        self.assertEqual(
            self.oc_calls[0]["model"],
            "amazon-bedrock/us.anthropic.claude-opus-4-7",
        )

    def test_openrouter_routes_to_opencode_full_model_unchanged(self):
        run_agent(**_kwargs(model="openrouter/anthropic/claude-opus-4-8"))
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(self.oc_calls[0]["model"], "openrouter/anthropic/claude-opus-4-8")

    def test_agent_forwarded_unchanged_to_opencode(self):
        run_agent(**_kwargs(agent="brainstorm", model="lmstudio/model"))
        self.assertEqual(self.oc_calls[0]["agent"], "brainstorm")

    def test_agent_forwarded_unchanged_to_claude(self):
        run_agent(**_kwargs(agent="brainstorm", model="claude-code/sonnet"))
        self.assertEqual(self.claude_calls[0]["agent"], "brainstorm")

    def test_kwargs_forwarded_to_claude(self):
        run_agent(
            agent="planner",
            model="claude-code/opus",
            message="plan this",
            cwd="/tmp/work",
            log_path="logs/p.json",
            timeout=99,
        )
        c = self.claude_calls[0]
        self.assertEqual(c["message"], "plan this")
        self.assertEqual(c["cwd"], "/tmp/work")
        self.assertEqual(c["log_path"], "logs/p.json")
        self.assertEqual(c["timeout"], 99)

    def test_kwargs_forwarded_to_opencode(self):
        run_agent(
            agent="executor",
            model="lmstudio/model",
            message="do it",
            cwd="/tmp/cwd",
            log_path="logs/e.json",
            timeout=42,
        )
        k = self.oc_calls[0]
        self.assertEqual(k["message"], "do it")
        self.assertEqual(k["cwd"], "/tmp/cwd")
        self.assertEqual(k["log_path"], "logs/e.json")
        self.assertEqual(k["timeout"], 42)

    def test_mixed_roles_route_independently_by_model(self):
        run_agent(**_kwargs(agent="planner", model="claude-code/opus"))
        run_agent(**_kwargs(agent="executor", model="lmstudio/qwen3.6"))
        run_agent(**_kwargs(agent="reviewer", model="claude-code/sonnet"))
        self.assertEqual(len(self.claude_calls), 2)
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual([c["model"] for c in self.claude_calls], ["opus", "sonnet"])
        self.assertEqual(self.oc_calls[0]["model"], "lmstudio/qwen3.6")


# --------------------------------------------------------------------------- #
# 6. Unknown provider → error RunResult, never raises
# --------------------------------------------------------------------------- #


class TestUnknownProvider(unittest.TestCase):
    _UNKNOWN = "xyz-totally-unknown-provider"

    def test_does_not_raise(self):
        try:
            run_agent(**_kwargs(model=f"{self._UNKNOWN}/model"))
        except Exception as e:
            self.fail(f"run_agent raised unexpectedly: {type(e).__name__}: {e}")

    def test_ok_is_false(self):
        r = run_agent(**_kwargs(model=f"{self._UNKNOWN}/model"))
        self.assertFalse(r.ok)

    def test_error_names_the_provider(self):
        r = run_agent(**_kwargs(model=f"{self._UNKNOWN}/model"))
        self.assertIsNotNone(r.error)
        self.assertIn(self._UNKNOWN, r.error)

    def test_returncode_is_nonzero(self):
        r = run_agent(**_kwargs(model=f"{self._UNKNOWN}/model"))
        self.assertNotEqual(r.returncode, 0)

    def test_log_path_is_preserved(self):
        r = run_agent(**_kwargs(model=f"{self._UNKNOWN}/model", log_path="expected.log"))
        self.assertEqual(r.log_path, "expected.log")

    def test_timed_out_is_false(self):
        r = run_agent(**_kwargs(model=f"{self._UNKNOWN}/model"))
        self.assertFalse(r.timed_out)

    def test_bare_unknown_model_string_no_slash(self):
        r = run_agent(**_kwargs(model="bareunknown"))
        self.assertFalse(r.ok)
        self.assertIsNotNone(r.error)

    def test_different_unknown_models_each_produce_error(self):
        for model in ("ghost/x", "phantom/y", "shadow/z"):
            with self.subTest(model=model):
                r = run_agent(**_kwargs(model=model))
                self.assertFalse(r.ok)


# --------------------------------------------------------------------------- #
# 7. No-elif extensibility — register a dummy runtime, route to it
# --------------------------------------------------------------------------- #


class TestRegistryExtensibility(_RegistryGuard, unittest.TestCase):
    def _make_capturing_runtime(self, name, provider, calls):
        class _CaptureRuntime:
            pass

        _CaptureRuntime.name = name
        _CaptureRuntime.providers = frozenset([provider])

        def run(self, *, agent, model, message, cwd, log_path, timeout):
            calls.append({"agent": agent, "model": model})
            return RunResult(returncode=0, text=f"from-{name}")

        _CaptureRuntime.run = run
        _CaptureRuntime.system_prompt_for = lambda self, agent: None
        return _CaptureRuntime()

    def test_register_dummy_then_route_to_it(self):
        calls = []
        inst = self._make_capturing_runtime("dummy-rt", "dummy-provider", calls)
        rt.register(inst)

        result = run_agent(**_kwargs(model="dummy-provider/some-model"))

        self.assertEqual(len(calls), 1, "dummy runtime's run() was not called")
        self.assertEqual(calls[0]["model"], "dummy-provider/some-model")
        self.assertEqual(result.text, "from-dummy-rt")

    def test_dummy_run_receives_full_model_string_unchanged(self):
        calls = []
        inst = self._make_capturing_runtime("passthru", "test-prov", calls)
        rt.register(inst)

        run_agent(**_kwargs(model="test-prov/vendor/specific-model"))
        self.assertEqual(calls[0]["model"], "test-prov/vendor/specific-model")

    def test_newly_registered_resolves_via_runtime_for_model(self):
        from director.opencode import runtime_for_model

        calls = []
        inst = self._make_capturing_runtime("probe-rt", "probe-prov", calls)
        rt.register(inst)

        resolved = runtime_for_model("probe-prov/x")
        self.assertIs(resolved, inst)

    def test_existing_runtimes_unaffected_by_new_registration(self):
        calls = []
        inst = self._make_capturing_runtime("extra-rt", "extra-prov", calls)
        rt.register(inst)

        # lmstudio must still resolve to OpenCodeRuntime
        existing = rt.resolve("lmstudio")
        self.assertIsInstance(existing, oc.OpenCodeRuntime)


# --------------------------------------------------------------------------- #
# 8. Registration collision raises ValueError
# --------------------------------------------------------------------------- #


class TestRegistryCollision(_RegistryGuard, unittest.TestCase):
    def _make_rt(self, name, providers):
        class _R:
            pass

        _R.name = name
        _R.providers = frozenset(providers)
        _R.run = lambda self, **kw: RunResult(returncode=0)
        _R.system_prompt_for = lambda self, agent: None
        return _R()

    def test_collision_raises_value_error(self):
        rt.register(self._make_rt("alpha", ["col-prov"]))
        with self.assertRaises(ValueError):
            rt.register(self._make_rt("beta", ["col-prov"]))

    def test_collision_error_names_the_colliding_provider(self):
        rt.register(self._make_rt("first", ["shared-key"]))
        with self.assertRaises(ValueError) as ctx:
            rt.register(self._make_rt("second", ["shared-key"]))
        self.assertIn("shared-key", str(ctx.exception))

    def test_collision_does_not_overwrite_original(self):
        r1 = self._make_rt("original", ["my-prov"])
        rt.register(r1)
        with contextlib.suppress(ValueError):
            rt.register(self._make_rt("interloper", ["my-prov"]))
        self.assertIs(rt._REGISTRY.get("my-prov"), r1)

    def test_collision_at_existing_opencode_provider_raises(self):
        """Trying to register a new runtime for an already-owned provider raises."""
        with self.assertRaises(ValueError):
            rt.register(self._make_rt("duplicate", ["lmstudio"]))


# --------------------------------------------------------------------------- #
# 9. Monkeypatchability
# --------------------------------------------------------------------------- #


class TestMonkeyPatchability(unittest.TestCase):
    def test_run_agent_is_writable_module_attribute(self):
        orig = oc.run_agent
        try:
            oc.run_agent = lambda **kw: RunResult(returncode=0, text="patched-ra")
            result = oc.run_agent(**_kwargs())
            self.assertEqual(result.text, "patched-ra")
        finally:
            oc.run_agent = orig

    def test_run_agent_restored_after_patch(self):
        orig = oc.run_agent
        sentinel = object()
        oc.run_agent = lambda **kw: sentinel
        oc.run_agent = orig
        self.assertIs(oc.run_agent, orig)

    def test_run_opencode_monkeypatch_honored_by_dispatch(self):
        """OpenCodeRuntime.run() calls _run_opencode by bare name in oc module."""
        calls = []
        orig = oc._run_opencode
        try:
            oc._run_opencode = lambda **kw: (
                calls.append(kw),
                RunResult(returncode=0, text="oc-patched"),
            )[1]
            result = run_agent(**_kwargs(model="lmstudio/some-model"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.text, "oc-patched")
        finally:
            oc._run_opencode = orig

    def test_run_opencode_patch_receives_full_model_string(self):
        """OpenCodeRuntime does NOT strip the provider segment."""
        received_model = []
        orig = oc._run_opencode
        try:
            oc._run_opencode = lambda **kw: (
                received_model.append(kw["model"]),
                RunResult(returncode=0),
            )[1]
            run_agent(**_kwargs(model="openrouter/anthropic/model"))
        finally:
            oc._run_opencode = orig
        self.assertEqual(received_model, ["openrouter/anthropic/model"])

    def test_run_claude_monkeypatch_honored_by_dispatch(self):
        """ClaudeCodeRuntime.run() calls run_claude by bare name in cc module."""
        calls = []
        orig = cc.run_claude
        try:
            cc.run_claude = lambda **kw: (
                calls.append(kw),
                RunResult(returncode=0, text="cc-patched"),
            )[1]
            result = run_agent(**_kwargs(model="claude-code/opus"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.text, "cc-patched")
        finally:
            cc.run_claude = orig

    def test_run_claude_patch_receives_stripped_model(self):
        """ClaudeCodeRuntime strips the leading 'claude-code/' segment."""
        received_model = []
        orig = cc.run_claude
        try:
            cc.run_claude = lambda **kw: (
                received_model.append(kw["model"]),
                RunResult(returncode=0),
            )[1]
            run_agent(**_kwargs(model="claude-code/sonnet-3-7"))
        finally:
            cc.run_claude = orig
        self.assertEqual(received_model, ["sonnet-3-7"])

    def test_run_opencode_stays_writable(self):
        """_run_opencode is a plain module attribute that can be reassigned."""
        orig = oc._run_opencode
        oc._run_opencode = orig  # no-op must not raise
        self.assertIs(oc._run_opencode, orig)


if __name__ == "__main__":
    unittest.main()
