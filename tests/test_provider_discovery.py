"""Acceptance tests for Provider.discover_models and providers() helper.

Covers:
  - Provider Protocol declares discover_models callable with correct signature
  - discover_models docstring includes required contract language
  - providers() exists, returns a list, deduplicates by instance identity,
    preserves stable registration order, and does not mutate the registry
  - provider_for_model on an unregistered provider still returns None (no fallback)

Run: python3 -m unittest tests.test_provider_discovery -v
"""

import importlib
import inspect
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.provider as rt

# --------------------------------------------------------------------------- #
# helpers (mirrors conventions in test_provider.py)
# --------------------------------------------------------------------------- #


def tearDownModule():
    """Restore built-in provider modules after tests that reload director.provider."""
    importlib.reload(rt)
    import director.claudecode as cc
    import director.codex as codex
    import director.opencode as oc

    importlib.reload(cc)
    importlib.reload(oc)
    importlib.reload(codex)


def _fresh():
    """Reload director.provider to get a pristine _REGISTRY."""
    importlib.reload(rt)
    return rt


def _make_rt(name, discover_returns=None):
    """Build a minimal conforming Provider stub that also implements discover_models."""

    class _FakeProvider:
        pass

    _FakeProvider.name = name

    _discover = discover_returns if discover_returns is not None else []

    def run(self, *, agent, model, message, cwd, log_path, timeout):
        return rt.RunResult(returncode=0)

    def system_prompt_for(self, agent):
        return None

    def discover_models(self):
        return list(_discover)

    _FakeProvider.run = run
    _FakeProvider.system_prompt_for = system_prompt_for
    _FakeProvider.discover_models = discover_models
    return _FakeProvider()


# --------------------------------------------------------------------------- #
# Provider Protocol — discover_models declaration
# --------------------------------------------------------------------------- #


class TestRuntimeProtocolDiscoverModels(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_protocol_has_discover_models_callable(self):
        self.assertTrue(
            callable(getattr(rt.Provider, "discover_models", None)),
            "Provider Protocol must declare discover_models as a callable",
        )

    def test_discover_models_signature_takes_only_self(self):
        sig = inspect.signature(rt.Provider.discover_models)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params,
            ["self"],
            "Provider.discover_models must accept only 'self' (no extra params)",
        )

    def test_discover_models_has_docstring(self):
        doc = getattr(rt.Provider.discover_models, "__doc__", None) or ""
        self.assertTrue(
            doc.strip(),
            "Provider.discover_models must have a docstring",
        )

    def test_discover_models_docstring_mentions_additive(self):
        doc = (rt.Provider.discover_models.__doc__ or "").lower()
        self.assertIn(
            "additive",
            doc,
            "discover_models docstring must state it is ADDITIVE",
        )

    def test_discover_models_docstring_mentions_init_time(self):
        doc = (rt.Provider.discover_models.__doc__ or "").lower()
        self.assertTrue(
            "init-time" in doc or "init time" in doc,
            "discover_models docstring must state it is INIT-TIME-ONLY",
        )

    def test_discover_models_docstring_mentions_not_used_by_resolution(self):
        doc = (rt.Provider.discover_models.__doc__ or "").lower()
        self.assertIn(
            "resolution",
            doc,
            "discover_models docstring must state it is NOT used by resolution",
        )

    def test_discover_models_docstring_mentions_provider_model_format(self):
        doc = rt.Provider.discover_models.__doc__ or ""
        # Must mention the "<provider>/<model>" tier string concept
        self.assertTrue(
            "/" in doc,
            "discover_models docstring must reference provider/model tier strings",
        )

    def test_discover_models_docstring_mentions_empty_list_when_unavailable(self):
        doc = (rt.Provider.discover_models.__doc__ or "").lower()
        self.assertIn(
            "empty list",
            doc,
            "discover_models docstring must state it returns empty list when unavailable",
        )

    def test_discover_models_docstring_mentions_must_never_raise(self):
        doc = (rt.Provider.discover_models.__doc__ or "").lower()
        self.assertTrue(
            "never raise" in doc or "must never raise" in doc,
            "discover_models docstring must state it MUST NEVER raise",
        )


# --------------------------------------------------------------------------- #
# providers() — module surface
# --------------------------------------------------------------------------- #


class TestRuntimesHelperExists(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_providers_function_exists(self):
        self.assertTrue(
            callable(getattr(rt, "providers", None)),
            "director.provider must expose a public providers() function",
        )

    def test_providers_takes_no_arguments(self):
        sig = inspect.signature(rt.providers)
        params = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
        self.assertEqual(
            params,
            [],
            "providers() must take no required arguments",
        )

    def test_providers_has_docstring(self):
        doc = getattr(rt.providers, "__doc__", None) or ""
        self.assertTrue(doc.strip(), "providers() must have a docstring")

    def test_module_exports_runtimes_name(self):
        self.assertIn(
            "providers",
            dir(rt),
            "providers must be accessible on the director.provider module",
        )


# --------------------------------------------------------------------------- #
# providers() — behavior: empty registry
# --------------------------------------------------------------------------- #


class TestRuntimesEmptyRegistry(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_returns_empty_list_when_no_runtimes_registered(self):
        result = rt.providers()
        self.assertEqual(result, [])

    def test_returns_a_list(self):
        self.assertIsInstance(rt.providers(), list)


# --------------------------------------------------------------------------- #
# providers() — behavior: single provider, single provider
# --------------------------------------------------------------------------- #


class TestRuntimesSingleRuntime(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_single_registered_runtime_appears_once(self):
        fake = _make_rt("prov-a")
        rt.register(fake)
        result = rt.providers()
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], fake)

    def test_returns_list_not_generator_or_set(self):
        fake = _make_rt("prov-b")
        rt.register(fake)
        self.assertIsInstance(rt.providers(), list)


# --------------------------------------------------------------------------- #
# providers() — behavior: one registry entry per provider name
# --------------------------------------------------------------------------- #


class TestRuntimesDeduplication(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_one_provider_yields_one_entry(self):
        shared = _make_rt("shared")
        rt.register(shared)
        result = rt.providers()
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], shared)

    def test_distinct_provider_names_each_appear(self):
        a = _make_rt("provA")
        b = _make_rt("provB")
        rt.register(a)
        rt.register(b)
        result = rt.providers()
        self.assertEqual(len(result), 2)
        self.assertIn(a, result)
        self.assertIn(b, result)


# --------------------------------------------------------------------------- #
# providers() — stable registration order
# --------------------------------------------------------------------------- #


class TestRuntimesOrder(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_two_runtimes_returned_in_registration_order(self):
        first = _make_rt("pf")
        second = _make_rt("ps")
        rt.register(first)
        rt.register(second)
        result = rt.providers()
        self.assertEqual(result, [first, second])

    def test_three_runtimes_returned_in_registration_order(self):
        a = _make_rt("pa")
        b = _make_rt("pb")
        c = _make_rt("pc")
        rt.register(a)
        rt.register(b)
        rt.register(c)
        result = rt.providers()
        self.assertEqual(result, [a, b, c])

    def test_registration_order_is_provider_name_order(self):
        early = _make_rt("ea")
        middle = _make_rt("m1")
        late = _make_rt("la")
        rt.register(early)
        rt.register(middle)
        rt.register(late)
        result = rt.providers()
        self.assertEqual(result, [early, middle, late])


# --------------------------------------------------------------------------- #
# providers() — does not mutate registry
# --------------------------------------------------------------------------- #


class TestRuntimesNoSideEffects(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_calling_runtimes_does_not_change_registry(self):
        fake = _make_rt("pu")
        rt.register(fake)
        before = dict(rt._REGISTRY)
        rt.providers()
        self.assertEqual(rt._REGISTRY, before)

    def test_mutating_returned_list_does_not_affect_registry(self):
        fake = _make_rt("ps")
        rt.register(fake)
        result = rt.providers()
        result.clear()
        self.assertIn("ps", rt._REGISTRY)

    def test_repeated_calls_return_equal_lists(self):
        fake = _make_rt("pr")
        rt.register(fake)
        self.assertEqual(rt.providers(), rt.providers())


# --------------------------------------------------------------------------- #
# Regression: provider_for_model unknown provider still returns None
# --------------------------------------------------------------------------- #


class TestRuntimeForModelNoFallback(unittest.TestCase):
    """Ensure adding providers() didn't introduce a silent default fallback."""

    def setUp(self):
        _fresh()

    def test_unknown_provider_still_returns_none(self):
        self.assertIsNone(
            rt.provider_for_model("unknown/x"),
            "provider_for_model must return None for an unregistered provider — no silent fallback",
        )

    def test_empty_registry_unknown_returns_none(self):
        self.assertIsNone(rt.provider_for_model("ghost/model"))

    def test_known_provider_unaffected(self):
        fake = _make_rt("known-prov")
        rt.register(fake)
        self.assertIs(rt.provider_for_model("known-prov/anything"), fake)
        self.assertIsNone(rt.provider_for_model("other/anything"))


if __name__ == "__main__":
    unittest.main()
