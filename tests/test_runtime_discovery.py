"""Acceptance tests for Runtime.discover_models and runtimes() helper.

Covers:
  - Runtime Protocol declares discover_models callable with correct signature
  - discover_models docstring includes required contract language
  - runtimes() exists, returns a list, deduplicates by instance identity,
    preserves stable registration order, and does not mutate the registry
  - runtime_for_model on an unregistered provider still returns None (no fallback)

Run: python3 -m unittest tests.test_runtime_discovery -v
"""

import importlib
import inspect
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.runtime as rt

# --------------------------------------------------------------------------- #
# helpers (mirrors conventions in test_runtime.py)
# --------------------------------------------------------------------------- #


def _fresh():
    """Reload director.runtime to get a pristine _REGISTRY."""
    importlib.reload(rt)
    return rt


def _make_rt(name, providers, discover_returns=None):
    """Build a minimal conforming Runtime stub that also implements discover_models."""

    class _FakeRuntime:
        pass

    _FakeRuntime.name = name
    _FakeRuntime.providers = frozenset(providers)

    _discover = discover_returns if discover_returns is not None else []

    def run(self, *, agent, model, message, cwd, log_path, timeout):
        return rt.RunResult(returncode=0)

    def system_prompt_for(self, agent):
        return None

    def discover_models(self):
        return list(_discover)

    _FakeRuntime.run = run
    _FakeRuntime.system_prompt_for = system_prompt_for
    _FakeRuntime.discover_models = discover_models
    return _FakeRuntime()


# --------------------------------------------------------------------------- #
# Runtime Protocol — discover_models declaration
# --------------------------------------------------------------------------- #


class TestRuntimeProtocolDiscoverModels(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_protocol_has_discover_models_callable(self):
        self.assertTrue(
            callable(getattr(rt.Runtime, "discover_models", None)),
            "Runtime Protocol must declare discover_models as a callable",
        )

    def test_discover_models_signature_takes_only_self(self):
        sig = inspect.signature(rt.Runtime.discover_models)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params,
            ["self"],
            "Runtime.discover_models must accept only 'self' (no extra params)",
        )

    def test_discover_models_has_docstring(self):
        doc = getattr(rt.Runtime.discover_models, "__doc__", None) or ""
        self.assertTrue(
            doc.strip(),
            "Runtime.discover_models must have a docstring",
        )

    def test_discover_models_docstring_mentions_additive(self):
        doc = (rt.Runtime.discover_models.__doc__ or "").lower()
        self.assertIn(
            "additive",
            doc,
            "discover_models docstring must state it is ADDITIVE",
        )

    def test_discover_models_docstring_mentions_init_time(self):
        doc = (rt.Runtime.discover_models.__doc__ or "").lower()
        self.assertTrue(
            "init-time" in doc or "init time" in doc,
            "discover_models docstring must state it is INIT-TIME-ONLY",
        )

    def test_discover_models_docstring_mentions_not_used_by_resolution(self):
        doc = (rt.Runtime.discover_models.__doc__ or "").lower()
        self.assertIn(
            "resolution",
            doc,
            "discover_models docstring must state it is NOT used by resolution",
        )

    def test_discover_models_docstring_mentions_provider_model_format(self):
        doc = rt.Runtime.discover_models.__doc__ or ""
        # Must mention the "<provider>/<model>" tier string concept
        self.assertTrue(
            "/" in doc,
            "discover_models docstring must reference provider/model tier strings",
        )

    def test_discover_models_docstring_mentions_empty_list_when_unavailable(self):
        doc = (rt.Runtime.discover_models.__doc__ or "").lower()
        self.assertIn(
            "empty list",
            doc,
            "discover_models docstring must state it returns empty list when unavailable",
        )

    def test_discover_models_docstring_mentions_must_never_raise(self):
        doc = (rt.Runtime.discover_models.__doc__ or "").lower()
        self.assertTrue(
            "never raise" in doc or "must never raise" in doc,
            "discover_models docstring must state it MUST NEVER raise",
        )


# --------------------------------------------------------------------------- #
# runtimes() — module surface
# --------------------------------------------------------------------------- #


class TestRuntimesHelperExists(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_runtimes_function_exists(self):
        self.assertTrue(
            callable(getattr(rt, "runtimes", None)),
            "director.runtime must expose a public runtimes() function",
        )

    def test_runtimes_takes_no_arguments(self):
        sig = inspect.signature(rt.runtimes)
        params = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
        self.assertEqual(
            params,
            [],
            "runtimes() must take no required arguments",
        )

    def test_runtimes_has_docstring(self):
        doc = getattr(rt.runtimes, "__doc__", None) or ""
        self.assertTrue(doc.strip(), "runtimes() must have a docstring")

    def test_module_exports_runtimes_name(self):
        self.assertIn(
            "runtimes",
            dir(rt),
            "runtimes must be accessible on the director.runtime module",
        )


# --------------------------------------------------------------------------- #
# runtimes() — behavior: empty registry
# --------------------------------------------------------------------------- #


class TestRuntimesEmptyRegistry(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_returns_empty_list_when_no_runtimes_registered(self):
        result = rt.runtimes()
        self.assertEqual(result, [])

    def test_returns_a_list(self):
        self.assertIsInstance(rt.runtimes(), list)


# --------------------------------------------------------------------------- #
# runtimes() — behavior: single runtime, single provider
# --------------------------------------------------------------------------- #


class TestRuntimesSingleRuntime(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_single_registered_runtime_appears_once(self):
        fake = _make_rt("solo", ["prov-a"])
        rt.register(fake)
        result = rt.runtimes()
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], fake)

    def test_returns_list_not_generator_or_set(self):
        fake = _make_rt("solo", ["prov-b"])
        rt.register(fake)
        self.assertIsInstance(rt.runtimes(), list)


# --------------------------------------------------------------------------- #
# runtimes() — deduplication: one runtime, multiple providers
# --------------------------------------------------------------------------- #


class TestRuntimesDeduplication(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_one_runtime_two_providers_yields_one_entry(self):
        shared = _make_rt("shared", ["provX", "provY"])
        rt.register(shared)
        result = rt.runtimes()
        self.assertEqual(
            len(result),
            1,
            "A single runtime registered under two providers must appear exactly once",
        )
        self.assertIs(result[0], shared)

    def test_one_runtime_three_providers_yields_one_entry(self):
        shared = _make_rt("triple", ["p1", "p2", "p3"])
        rt.register(shared)
        self.assertEqual(len(rt.runtimes()), 1)

    def test_dedup_is_by_instance_identity(self):
        # Two distinct objects with same name should each appear
        a = _make_rt("rt-a", ["provA"])
        b = _make_rt("rt-b", ["provB"])
        rt.register(a)
        rt.register(b)
        result = rt.runtimes()
        self.assertEqual(len(result), 2)
        self.assertIn(a, result)
        self.assertIn(b, result)


# --------------------------------------------------------------------------- #
# runtimes() — stable registration order
# --------------------------------------------------------------------------- #


class TestRuntimesOrder(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_two_runtimes_returned_in_registration_order(self):
        first = _make_rt("first", ["pf"])
        second = _make_rt("second", ["ps"])
        rt.register(first)
        rt.register(second)
        result = rt.runtimes()
        self.assertEqual(result, [first, second])

    def test_three_runtimes_returned_in_registration_order(self):
        a = _make_rt("a", ["pa"])
        b = _make_rt("b", ["pb"])
        c = _make_rt("c", ["pc"])
        rt.register(a)
        rt.register(b)
        rt.register(c)
        result = rt.runtimes()
        self.assertEqual(result, [a, b, c])

    def test_first_seen_order_for_multi_provider_runtime(self):
        # When one runtime claims two providers, its position is the FIRST registration.
        early = _make_rt("early", ["ea"])
        multi = _make_rt("multi", ["m1", "m2"])
        late = _make_rt("late", ["la"])
        rt.register(early)
        rt.register(multi)
        rt.register(late)
        result = rt.runtimes()
        self.assertEqual(result, [early, multi, late])


# --------------------------------------------------------------------------- #
# runtimes() — does not mutate registry
# --------------------------------------------------------------------------- #


class TestRuntimesNoSideEffects(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_calling_runtimes_does_not_change_registry(self):
        fake = _make_rt("unchanged", ["pu"])
        rt.register(fake)
        before = dict(rt._REGISTRY)
        rt.runtimes()
        self.assertEqual(rt._REGISTRY, before)

    def test_mutating_returned_list_does_not_affect_registry(self):
        fake = _make_rt("stable", ["ps"])
        rt.register(fake)
        result = rt.runtimes()
        result.clear()
        self.assertIn("ps", rt._REGISTRY)

    def test_repeated_calls_return_equal_lists(self):
        fake = _make_rt("rep", ["pr"])
        rt.register(fake)
        self.assertEqual(rt.runtimes(), rt.runtimes())


# --------------------------------------------------------------------------- #
# Regression: runtime_for_model unknown provider still returns None
# --------------------------------------------------------------------------- #


class TestRuntimeForModelNoFallback(unittest.TestCase):
    """Ensure adding runtimes() didn't introduce a silent default fallback."""

    def setUp(self):
        _fresh()

    def test_unknown_provider_still_returns_none(self):
        self.assertIsNone(
            rt.runtime_for_model("unknown/x"),
            "runtime_for_model must return None for an unregistered provider — no silent fallback",
        )

    def test_empty_registry_unknown_returns_none(self):
        self.assertIsNone(rt.runtime_for_model("ghost/model"))

    def test_known_provider_unaffected(self):
        fake = _make_rt("known", ["known-prov"])
        rt.register(fake)
        self.assertIs(rt.runtime_for_model("known-prov/anything"), fake)
        self.assertIsNone(rt.runtime_for_model("other/anything"))


if __name__ == "__main__":
    unittest.main()
