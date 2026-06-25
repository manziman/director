"""Acceptance tests for director.runtime — shared runtime primitives + registry.

These tests cover: _CLEAN_ENV, RunResult (dataclass + .ok property + safe defaults),
Runtime (Protocol members + conformance), and the _REGISTRY/_register/resolve/
runtime_for_model API. No real CLIs, network, or external state is touched.

Run: python3 -m unittest discover -s tests -p test_runtime.py -q
"""

import importlib
import inspect
import os
import pathlib
import sys
import typing
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import contextlib

import director.runtime as rt  # noqa: E402

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fresh():
    """Reload director.runtime to get a pristine _REGISTRY (empty dict)."""
    importlib.reload(rt)
    return rt


def _make_rt(name, providers):
    """Build a minimal conforming Runtime object for registry tests."""

    class _FakeRuntime:
        pass

    _FakeRuntime.name = name
    _FakeRuntime.providers = frozenset(providers)

    def run(self, *, agent, model, message, cwd, log_path, timeout):
        return rt.RunResult(returncode=0)

    def system_prompt_for(self, agent):
        return None

    _FakeRuntime.run = run
    _FakeRuntime.system_prompt_for = system_prompt_for
    return _FakeRuntime()


# --------------------------------------------------------------------------- #
# Module surface — no director imports
# --------------------------------------------------------------------------- #


class TestModuleSurface(unittest.TestCase):
    def test_module_importable(self):
        import director.runtime  # noqa: F401

    def test_no_director_package_imports(self):
        """director.runtime MUST NOT import from the director package."""
        import ast

        src = pathlib.Path(__file__).resolve().parent.parent / "director" / "runtime.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertFalse(
                    node.module.startswith("director"),
                    f"director.runtime imports from director.*: found 'from {node.module} import ...'",
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith("director"),
                        f"director.runtime imports director.*: found 'import {alias.name}'",
                    )

    def test_exports_required_names(self):
        for name in (
            "_CLEAN_ENV",
            "RunResult",
            "Runtime",
            "_REGISTRY",
            "register",
            "resolve",
            "runtime_for_model",
        ):
            self.assertTrue(
                hasattr(rt, name),
                f"director.runtime is missing expected name: {name}",
            )


# --------------------------------------------------------------------------- #
# _CLEAN_ENV
# --------------------------------------------------------------------------- #


class TestCleanEnv(unittest.TestCase):
    def test_is_dict(self):
        self.assertIsInstance(rt._CLEAN_ENV, dict)

    def test_has_pythondontwritebytecode_one(self):
        self.assertEqual(rt._CLEAN_ENV.get("PYTHONDONTWRITEBYTECODE"), "1")

    def test_is_superset_of_os_environ(self):
        """_CLEAN_ENV is built from {**os.environ, ...} so every env key must appear."""
        for key in os.environ:
            self.assertIn(
                key,
                rt._CLEAN_ENV,
                f"_CLEAN_ENV is missing os.environ key: {key!r}",
            )


# --------------------------------------------------------------------------- #
# RunResult — dataclass fields, safe defaults, .ok property
# --------------------------------------------------------------------------- #


class TestRunResultFields(unittest.TestCase):
    def test_is_dataclass(self):
        import dataclasses

        self.assertTrue(dataclasses.is_dataclass(rt.RunResult))

    def test_has_exactly_ten_fields(self):
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(rt.RunResult)}
        expected = {
            "returncode",
            "text",
            "tokens",
            "cost_reported",
            "n_steps",
            "tool_calls",
            "tool_events",
            "error",
            "timed_out",
            "log_path",
        }
        self.assertEqual(field_names, expected)

    def test_partial_constructor_succeeds(self):
        """The critical A8 requirement: keyword-only construction with minimal args."""
        r = rt.RunResult(returncode=2, error="boom", timed_out=False, log_path="/tmp/x")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.error, "boom")
        self.assertFalse(r.timed_out)
        self.assertEqual(r.log_path, "/tmp/x")

    def test_returncode_only_construction(self):
        r = rt.RunResult(returncode=0)
        self.assertEqual(r.returncode, 0)

    def test_text_default_is_empty_string(self):
        r = rt.RunResult(returncode=0)
        self.assertEqual(r.text, "")

    def test_tokens_default_is_dict(self):
        r = rt.RunResult(returncode=0)
        self.assertIsInstance(r.tokens, dict)

    def test_cost_reported_default_is_zero(self):
        r = rt.RunResult(returncode=0)
        self.assertEqual(r.cost_reported, 0.0)

    def test_n_steps_default_is_zero(self):
        r = rt.RunResult(returncode=0)
        self.assertEqual(r.n_steps, 0)

    def test_tool_calls_default_is_empty_list(self):
        r = rt.RunResult(returncode=0)
        self.assertIsInstance(r.tool_calls, list)
        self.assertEqual(r.tool_calls, [])

    def test_tool_events_default_is_empty_list(self):
        r = rt.RunResult(returncode=0)
        self.assertIsInstance(r.tool_events, list)
        self.assertEqual(r.tool_events, [])

    def test_error_default_is_none(self):
        r = rt.RunResult(returncode=0)
        self.assertIsNone(r.error)

    def test_timed_out_default_is_false(self):
        r = rt.RunResult(returncode=0)
        self.assertFalse(r.timed_out)

    def test_log_path_default_is_empty_string(self):
        r = rt.RunResult(returncode=0)
        self.assertEqual(r.log_path, "")

    def test_tool_calls_not_shared_across_instances(self):
        r1 = rt.RunResult(returncode=0)
        r2 = rt.RunResult(returncode=0)
        r1.tool_calls.append(("Bash", "ok"))
        self.assertEqual(r2.tool_calls, [])

    def test_tool_events_not_shared_across_instances(self):
        r1 = rt.RunResult(returncode=0)
        r2 = rt.RunResult(returncode=0)
        r1.tool_events.append({"name": "bash", "status": "ok", "blob": ""})
        self.assertEqual(r2.tool_events, [])

    def test_tokens_not_shared_across_instances(self):
        r1 = rt.RunResult(returncode=0)
        r2 = rt.RunResult(returncode=0)
        r1.tokens["input"] = 99
        self.assertNotIn("input", r2.tokens)


class TestRunResultOkProperty(unittest.TestCase):
    def test_ok_true_when_rc0_no_error_not_timed_out(self):
        r = rt.RunResult(returncode=0, error=None, timed_out=False)
        self.assertTrue(r.ok)

    def test_ok_false_when_returncode_nonzero(self):
        r = rt.RunResult(returncode=1)
        self.assertFalse(r.ok)

    def test_ok_false_when_error_is_set(self):
        r = rt.RunResult(returncode=0, error="something failed")
        self.assertFalse(r.ok)

    def test_ok_false_when_timed_out(self):
        r = rt.RunResult(returncode=0, timed_out=True)
        self.assertFalse(r.ok)

    def test_ok_false_when_timed_out_rc_124(self):
        r = rt.RunResult(returncode=124, timed_out=True)
        self.assertFalse(r.ok)

    def test_ok_false_when_error_and_nonzero_rc(self):
        r = rt.RunResult(returncode=2, error="boom", timed_out=False)
        self.assertFalse(r.ok)

    def test_ok_is_property_not_field(self):
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(rt.RunResult)}
        self.assertNotIn("ok", field_names)
        self.assertIsInstance(
            inspect.getattr_static(rt.RunResult, "ok"),
            property,
        )


# --------------------------------------------------------------------------- #
# Runtime Protocol
# --------------------------------------------------------------------------- #


class TestRuntimeProtocol(unittest.TestCase):
    def test_is_protocol(self):
        # typing.is_protocol is available in Python 3.12+; fall back for older
        if hasattr(typing, "is_protocol"):
            self.assertTrue(typing.is_protocol(rt.Runtime))
        else:
            self.assertTrue(getattr(rt.Runtime, "_is_protocol", False))

    def test_protocol_declares_name_annotation(self):
        ann = rt.Runtime.__annotations__
        self.assertIn("name", ann)

    def test_protocol_declares_providers_annotation(self):
        ann = rt.Runtime.__annotations__
        self.assertIn("providers", ann)

    def test_protocol_has_run_callable(self):
        self.assertTrue(callable(getattr(rt.Runtime, "run", None)))

    def test_protocol_has_system_prompt_for_callable(self):
        self.assertTrue(callable(getattr(rt.Runtime, "system_prompt_for", None)))

    def test_run_signature_has_all_required_kwargs(self):
        sig = inspect.signature(rt.Runtime.run)
        params = set(sig.parameters.keys()) - {"self"}
        for required in ("agent", "model", "message", "cwd", "log_path", "timeout"):
            self.assertIn(required, params, f"Runtime.run is missing kwarg: {required!r}")

    def test_system_prompt_for_signature_has_agent_param(self):
        sig = inspect.signature(rt.Runtime.system_prompt_for)
        self.assertIn("agent", sig.parameters)


# --------------------------------------------------------------------------- #
# _REGISTRY — initial state
# --------------------------------------------------------------------------- #


class TestRegistryInitialState(unittest.TestCase):
    def test_registry_is_dict(self):
        m = _fresh()
        self.assertIsInstance(m._REGISTRY, dict)

    def test_registry_starts_empty(self):
        m = _fresh()
        self.assertEqual(m._REGISTRY, {})


# --------------------------------------------------------------------------- #
# register()
# --------------------------------------------------------------------------- #


class TestRegister(unittest.TestCase):
    def setUp(self):
        _fresh()  # reset registry before each test

    def test_register_single_provider(self):
        fake = _make_rt("fake", ["myprov"])
        rt.register(fake)
        self.assertIn("myprov", rt._REGISTRY)
        self.assertIs(rt._REGISTRY["myprov"], fake)

    def test_register_multiple_providers(self):
        fake = _make_rt("multi", ["provA", "provB"])
        rt.register(fake)
        self.assertIs(rt._REGISTRY.get("provA"), fake)
        self.assertIs(rt._REGISTRY.get("provB"), fake)

    def test_register_two_non_overlapping_runtimes(self):
        rt1 = _make_rt("rt1", ["p1"])
        rt2 = _make_rt("rt2", ["p2"])
        rt.register(rt1)
        rt.register(rt2)
        self.assertIs(rt._REGISTRY["p1"], rt1)
        self.assertIs(rt._REGISTRY["p2"], rt2)

    def test_collision_raises_value_error(self):
        rt1 = _make_rt("first", ["shared"])
        rt2 = _make_rt("second", ["shared"])
        rt.register(rt1)
        with self.assertRaises(ValueError):
            rt.register(rt2)

    def test_collision_error_names_the_colliding_provider(self):
        rt1 = _make_rt("first", ["collision-key"])
        rt2 = _make_rt("second", ["collision-key"])
        rt.register(rt1)
        with self.assertRaises(ValueError) as ctx:
            rt.register(rt2)
        self.assertIn("collision-key", str(ctx.exception))

    def test_collision_error_names_both_runtimes(self):
        rt1 = _make_rt("runtime-alpha", ["collide"])
        rt2 = _make_rt("runtime-beta", ["collide"])
        rt.register(rt1)
        with self.assertRaises(ValueError) as ctx:
            rt.register(rt2)
        msg = str(ctx.exception)
        self.assertIn("runtime-alpha", msg)
        self.assertIn("runtime-beta", msg)

    def test_collision_does_not_overwrite_original(self):
        rt1 = _make_rt("original", ["overlap"])
        rt2 = _make_rt("newcomer", ["overlap"])
        rt.register(rt1)
        with contextlib.suppress(ValueError):
            rt.register(rt2)
        self.assertIs(rt._REGISTRY["overlap"], rt1)

    def test_check_before_insert(self):
        """Collision check happens BEFORE any insert for that provider."""
        rt1 = _make_rt("rt1", ["existing"])
        rt.register(rt1)
        # rt2 claims "existing" (collision) and "new-one"
        # "new-one" must not end up in registry if "existing" collides first
        # (iteration order of frozenset is not guaranteed, so we only verify
        # that the original entry is intact; we cannot require pB to be absent)
        rt2 = _make_rt("rt2", ["existing", "new-one"])
        with contextlib.suppress(ValueError):
            rt.register(rt2)
        # The colliding entry must not be overwritten
        self.assertIs(rt._REGISTRY.get("existing"), rt1)


# --------------------------------------------------------------------------- #
# resolve()
# --------------------------------------------------------------------------- #


class TestResolve(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_resolve_returns_registered_runtime(self):
        fake = _make_rt("fake", ["myprov"])
        rt.register(fake)
        self.assertIs(rt.resolve("myprov"), fake)

    def test_resolve_returns_none_for_unknown(self):
        self.assertIsNone(rt.resolve("unknown"))

    def test_resolve_exact_match_no_prefix(self):
        fake = _make_rt("fake", ["exact"])
        rt.register(fake)
        self.assertIsNone(rt.resolve("exact/model"))

    def test_resolve_exact_match_no_suffix(self):
        fake = _make_rt("fake", ["exact"])
        rt.register(fake)
        self.assertIsNone(rt.resolve("not-exact"))

    def test_resolve_after_reload_empty(self):
        m = _fresh()
        self.assertIsNone(m.resolve("anything"))


# --------------------------------------------------------------------------- #
# runtime_for_model()
# --------------------------------------------------------------------------- #


class TestRuntimeForModel(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_provider_slash_model_routes_correctly(self):
        fake = _make_rt("fake", ["myprovider"])
        rt.register(fake)
        self.assertIs(rt.runtime_for_model("myprovider/mymodel"), fake)

    def test_bare_model_no_slash_uses_whole_string_as_provider(self):
        fake = _make_rt("fake", ["baremodel"])
        rt.register(fake)
        self.assertIs(rt.runtime_for_model("baremodel"), fake)

    def test_only_first_segment_matters(self):
        fake = _make_rt("fake", ["provider"])
        rt.register(fake)
        self.assertIs(rt.runtime_for_model("provider/vendor/modelname"), fake)

    def test_unknown_provider_returns_none(self):
        self.assertIsNone(rt.runtime_for_model("unknown/model"))

    def test_unknown_bare_string_returns_none(self):
        self.assertIsNone(rt.runtime_for_model("noprovider"))

    def test_different_providers_route_to_different_runtimes(self):
        rt1 = _make_rt("rt1", ["prov1"])
        rt2 = _make_rt("rt2", ["prov2"])
        rt.register(rt1)
        rt.register(rt2)
        self.assertIs(rt.runtime_for_model("prov1/model"), rt1)
        self.assertIs(rt.runtime_for_model("prov2/model"), rt2)

    def test_opencode_style_model_string(self):
        fake = _make_rt("oc", ["lmstudio"])
        rt.register(fake)
        self.assertIs(rt.runtime_for_model("lmstudio/qwen3.6-27b"), fake)

    def test_bedrock_style_model_string(self):
        fake = _make_rt("bedrock", ["amazon-bedrock"])
        rt.register(fake)
        self.assertIs(
            rt.runtime_for_model("amazon-bedrock/us.anthropic.claude-opus-4-7"),
            fake,
        )


if __name__ == "__main__":
    unittest.main()
