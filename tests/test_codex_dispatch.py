"""Registry-based dispatch tests for the Codex provider wiring.

Mirrors tests/test_dispatch.py, but for the Codex provider. Verifies that the
side-effect import added to the public dispatch module (director.opencode) is
what registers CodexProvider, and that run_agent routes ``codex/*`` tiers to
CodexProvider.run — which strips the leading ``codex/`` segment and delegates
to ``director.codex.run_codex`` by bare name, forwarding every other kwarg
unchanged. Also verifies dispatching a codex tier does not disturb the
claude-code / opencode routing (and vice versa).

The test command only matches ``test_*dispatch.py`` (NOT ``test_codex.py``), so
within this run director.codex is imported ONLY as a side effect of importing
director.opencode. That is the behavior under test.

Run: python3 -m unittest discover -s tests -p 'test_*dispatch.py' -q
"""

from __future__ import annotations

import importlib
import os
import pathlib
import subprocess
import sys
import unittest

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, _REPO_ROOT)


def _import_then_resolve_codex(import_stmt: str) -> subprocess.CompletedProcess:
    """Run `import_stmt` in a FRESH interpreter, then resolve('codex').

    Hermetic: a brand-new process with only the given import means codex can be
    registered ONLY as a side effect of that import — nothing else in the
    snippet imports director.codex. Deterministic (no network/time/random)."""
    snippet = (
        f"{import_stmt}\n"
        "import director.provider as p\n"
        "e = p.resolve('codex')\n"
        "assert e is not None, 'codex NOT registered by: %s'\n"
        "print(type(e).__name__, e.name)\n"
    ) % import_stmt
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": _REPO_ROOT},
    )


# NOTE: director.codex is deliberately NOT imported at module scope. Importing
# director.opencode below must be what pulls CodexProvider into the registry
# (via the side-effect import wired into director/opencode.py). Tests that need
# to monkeypatch run_codex import director.codex locally, inside the method.
import director.claudecode as cc  # noqa: E402
import director.opencode as oc  # noqa: E402
import director.provider as rt  # noqa: E402
from director.opencode import RunResult, run_agent  # noqa: E402


def setUpModule():
    """Refresh adapter modules so references are consistent after the sibling
    test module (test_dispatch.py) reloads the runtime/provider modules within
    the same process, and so the codex provider is not left stale."""
    global RunResult, run_agent

    import director.codex as _codex

    importlib.reload(rt)
    importlib.reload(cc)
    importlib.reload(oc)
    importlib.reload(_codex)

    from director.opencode import RunResult as _RunResult
    from director.opencode import run_agent as _run_agent

    RunResult = _RunResult
    run_agent = _run_agent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _kwargs(**extra):
    """Build a minimal valid kwargs dict for run_agent (codex tier by default)."""
    base = {
        "agent": "executor",
        "model": "codex/gpt-5-codex",
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


class _CodexDispatchMixin:
    """Monkeypatch director.codex.run_codex; record calls; restore after."""

    def setUp(self):
        super().setUp()
        import director.codex as codex_mod

        self.codex_mod = codex_mod
        self.codex_calls = []
        self._orig_run_codex = codex_mod.run_codex
        _cx = self.codex_calls
        codex_mod.run_codex = lambda **kw: (_cx.append(kw), _ok("codex"))[1]

    def tearDown(self):
        self.codex_mod.run_codex = self._orig_run_codex
        super().tearDown()


class _AllProvidersMixin:
    """Monkeypatch run_codex, _run_opencode AND run_claude; record which fires."""

    def setUp(self):
        super().setUp()
        import director.codex as codex_mod

        self.codex_mod = codex_mod
        self.codex_calls = []
        self.oc_calls = []
        self.claude_calls = []
        self._orig_run_codex = codex_mod.run_codex
        self._orig_run_oc = oc._run_opencode
        self._orig_run_claude = cc.run_claude
        _cx, _oc, _cc = self.codex_calls, self.oc_calls, self.claude_calls
        codex_mod.run_codex = lambda **kw: (_cx.append(kw), _ok("codex"))[1]
        oc._run_opencode = lambda **kw: (_oc.append(kw), _ok("oc"))[1]
        cc.run_claude = lambda **kw: (_cc.append(kw), _ok("claude"))[1]

    def tearDown(self):
        self.codex_mod.run_codex = self._orig_run_codex
        oc._run_opencode = self._orig_run_oc
        cc.run_claude = self._orig_run_claude
        super().tearDown()


# --------------------------------------------------------------------------- #
# (a) Registration via the director.opencode side-effect import
# --------------------------------------------------------------------------- #


class TestCodexRegisteredViaOpenCodeImport(unittest.TestCase):
    def test_importing_opencode_alone_registers_codex(self):
        """(a) Hermetic proof in a FRESH interpreter: importing director.opencode
        — and nothing else — must leave CodexProvider in the registry. This is
        the side-effect import wired into director/opencode.py. If that import
        line is absent, resolve('codex') is None and the subprocess exits non-zero.
        Deliberately does NOT import director.codex."""
        proc = _import_then_resolve_codex("import director.opencode")
        self.assertEqual(
            proc.returncode,
            0,
            msg=(
                "importing director.opencode did not register CodexProvider — "
                "side-effect `import director.codex` missing from "
                f"director/opencode.py.\nstderr:\n{proc.stderr}"
            ),
        )
        self.assertIn("CodexProvider codex", proc.stdout)

    def test_importing_init_alone_registers_codex(self):
        """The parallel side-effect import wired into director/init.py."""
        proc = _import_then_resolve_codex("import director.init")
        self.assertEqual(
            proc.returncode,
            0,
            msg=(
                "importing director.init did not register CodexProvider — "
                "side-effect `import director.codex` missing from "
                f"director/init.py.\nstderr:\n{proc.stderr}"
            ),
        )
        self.assertIn("CodexProvider codex", proc.stdout)

    def test_resolve_codex_returns_codex_provider_after_module_import(self):
        """Steady state in-process: 'codex' is registered and is a CodexProvider."""
        entry = rt.resolve("codex")
        self.assertIsNotNone(entry, "'codex' missing from registry")
        self.assertEqual(entry.name, "codex")
        # Asserted WITHOUT importing director.codex — proxy for isinstance(CodexProvider).
        self.assertEqual(type(entry).__name__, "CodexProvider")

    def test_provider_for_model_resolves_codex_tier(self):
        from director.opencode import provider_for_model

        resolved = provider_for_model("codex/gpt-5-codex")
        self.assertIsNotNone(resolved)
        self.assertIs(resolved, rt.resolve("codex"))
        self.assertEqual(resolved.name, "codex")

    def test_resolve_and_provider_for_model_agree_on_codex(self):
        from director.opencode import provider_for_model

        self.assertIs(rt.resolve("codex"), provider_for_model("codex/anything"))


# --------------------------------------------------------------------------- #
# (b) + (c) Dispatch routing: strip leading segment, forward every kwarg
# --------------------------------------------------------------------------- #


class TestCodexRouting(_CodexDispatchMixin, unittest.TestCase):
    def test_codex_simple_routes_to_run_codex_model_stripped(self):
        r = run_agent(**_kwargs(model="codex/gpt-5-codex"))
        self.assertEqual(r.text, "codex")
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(self.codex_calls[0]["model"], "gpt-5-codex")

    def test_codex_nested_model_only_first_segment_stripped(self):  # (c)
        run_agent(**_kwargs(model="codex/vendor/model"))
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(self.codex_calls[0]["model"], "vendor/model")

    def test_codex_three_level_remaining_slashes_preserved(self):
        run_agent(**_kwargs(model="codex/a/b/c"))
        self.assertEqual(self.codex_calls[0]["model"], "a/b/c")

    def test_agent_forwarded_unchanged(self):
        run_agent(**_kwargs(agent="brainstorm", model="codex/gpt-5-codex"))
        self.assertEqual(self.codex_calls[0]["agent"], "brainstorm")

    def test_all_other_kwargs_forwarded_unchanged(self):
        run_agent(
            agent="planner",
            model="codex/gpt-5-codex",
            message="plan this",
            cwd="/tmp/work",
            log_path="logs/p.json",
            timeout=99,
        )
        c = self.codex_calls[0]
        self.assertEqual(c["agent"], "planner")
        self.assertEqual(c["message"], "plan this")
        self.assertEqual(c["cwd"], "/tmp/work")
        self.assertEqual(c["log_path"], "logs/p.json")
        self.assertEqual(c["timeout"], 99)
        # exactly the six run_agent kwargs are forwarded — nothing added/dropped
        self.assertEqual(
            set(c),
            {"agent", "model", "message", "cwd", "log_path", "timeout"},
        )

    def test_result_is_run_codex_return_value(self):
        r = run_agent(**_kwargs())
        self.assertEqual(r.text, "codex")
        self.assertEqual(r.returncode, 0)


# --------------------------------------------------------------------------- #
# (d) Bare-name monkeypatch of run_codex is honored by dispatch
#     (mirrors test_run_claude_monkeypatch_honored_by_dispatch)
# --------------------------------------------------------------------------- #


class TestCodexMonkeyPatchability(unittest.TestCase):
    def test_run_codex_monkeypatch_honored_by_dispatch(self):
        """CodexProvider.run() calls run_codex by bare name in the codex module."""
        import director.codex as codex_mod

        calls = []
        orig = codex_mod.run_codex
        try:
            codex_mod.run_codex = lambda **kw: (
                calls.append(kw),
                RunResult(returncode=0, text="codex-patched"),
            )[1]
            result = run_agent(**_kwargs(model="codex/gpt-5-codex"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.text, "codex-patched")
        finally:
            codex_mod.run_codex = orig

    def test_run_codex_patch_receives_stripped_model_string(self):
        import director.codex as codex_mod

        received_model = []
        orig = codex_mod.run_codex
        try:
            codex_mod.run_codex = lambda **kw: (
                received_model.append(kw["model"]),
                RunResult(returncode=0),
            )[1]
            run_agent(**_kwargs(model="codex/vendor/deep/model"))
        finally:
            codex_mod.run_codex = orig
        self.assertEqual(received_model, ["vendor/deep/model"])


# --------------------------------------------------------------------------- #
# (e) Cross-provider isolation: a codex tier leaves claude-code / opencode
#     untouched, and vice versa.
# --------------------------------------------------------------------------- #


class TestCrossProviderIsolation(_AllProvidersMixin, unittest.TestCase):
    def test_codex_tier_does_not_touch_claude_or_opencode(self):
        run_agent(**_kwargs(model="codex/gpt-5-codex"))
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(len(self.oc_calls), 0)
        self.assertEqual(len(self.claude_calls), 0)

    def test_claude_tier_does_not_touch_codex(self):
        run_agent(**_kwargs(model="claude-code/opus"))
        self.assertEqual(len(self.claude_calls), 1)
        self.assertEqual(self.claude_calls[0]["model"], "opus")
        self.assertEqual(len(self.codex_calls), 0)
        self.assertEqual(len(self.oc_calls), 0)

    def test_opencode_tier_does_not_touch_codex(self):
        run_agent(**_kwargs(model="opencode/lmstudio/qwen3.6-27b"))
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(self.oc_calls[0]["model"], "lmstudio/qwen3.6-27b")
        self.assertEqual(len(self.codex_calls), 0)
        self.assertEqual(len(self.claude_calls), 0)

    def test_mixed_roles_route_independently_by_model(self):
        run_agent(**_kwargs(agent="planner", model="codex/gpt-5-codex"))
        run_agent(**_kwargs(agent="executor", model="claude-code/opus"))
        run_agent(**_kwargs(agent="reviewer", model="opencode/lmstudio/qwen3.6"))
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(len(self.claude_calls), 1)
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(self.codex_calls[0]["model"], "gpt-5-codex")
        self.assertEqual(self.claude_calls[0]["model"], "opus")
        self.assertEqual(self.oc_calls[0]["model"], "lmstudio/qwen3.6")


if __name__ == "__main__":
    unittest.main()
