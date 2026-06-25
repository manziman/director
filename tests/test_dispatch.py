"""Runtime selection via the tier model-string prefix.

`director.opencode.run_agent` chooses its backend purely from the `model` string:
a `claude-code/<model>` tier routes to the Claude Code runtime (with the prefix
stripped); anything else routes to OpenCode (the default). There is no separate
[runtime] config, no global state, and no `tier` argument — selection is a
property of the model string the call already carries.

These tests stub the two leaf runtimes (`_run_opencode` and a fake
`director.claudecode.run_claude`), so no real CLI/model/network is touched.
"""

import inspect
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import director.opencode as oc  # noqa: E402
from director.opencode import RunResult, run_agent, watch_it_fail  # noqa: E402


def _kwargs(**extra):
    base = {
        "agent": "executor",
        "model": "lmstudio/qwen3.6-27b-mtp",
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
        n_steps=1,
    )


class _StubRuntimesMixin:
    """Record opencode + claude invocations without touching real CLIs."""

    def setUp(self):
        self.oc_calls = []
        self.claude_calls = []
        self._orig_oc = oc._run_opencode
        oc._run_opencode = lambda **kw: (self.oc_calls.append(kw), _result("oc"))[1]

        fake = types.ModuleType("director.claudecode")
        claude_calls = self.claude_calls

        def run_claude(**kw):
            claude_calls.append(kw)
            return _result("claude")

        fake.run_claude = run_claude
        self._orig_cc = sys.modules.get("director.claudecode")
        sys.modules["director.claudecode"] = fake

    def tearDown(self):
        oc._run_opencode = self._orig_oc
        if self._orig_cc is None:
            sys.modules.pop("director.claudecode", None)
        else:
            sys.modules["director.claudecode"] = self._orig_cc


class TestModuleSurface(unittest.TestCase):
    def test_run_agent_has_exact_params(self):
        sig = inspect.signature(run_agent)
        self.assertEqual(
            set(sig.parameters),
            {"agent", "model", "message", "cwd", "log_path", "timeout"},
        )

    def test_no_runtime_global_or_setter(self):
        # The old [runtime]/set_runtime machinery is gone; selection is by prefix.
        self.assertFalse(hasattr(oc, "set_runtime"))
        self.assertFalse(hasattr(oc, "_RUNTIME"))
        self.assertEqual(oc.CLAUDE_PREFIX, "claude-code/")

    def test_watch_it_fail_still_callable(self):
        self.assertTrue(callable(watch_it_fail))


class TestPrefixDispatch(_StubRuntimesMixin, unittest.TestCase):
    def test_non_prefixed_model_routes_to_opencode(self):
        r = run_agent(**_kwargs(model="lmstudio/qwen3.6-27b-mtp"))
        self.assertEqual(r.text, "oc")
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(len(self.claude_calls), 0)
        # opencode receives the full provider/model string unchanged
        self.assertEqual(self.oc_calls[0]["model"], "lmstudio/qwen3.6-27b-mtp")

    def test_bedrock_model_routes_to_opencode(self):
        run_agent(**_kwargs(model="amazon-bedrock/us.anthropic.claude-opus-4-7"))
        self.assertEqual(len(self.oc_calls), 1)
        self.assertEqual(len(self.claude_calls), 0)

    def test_claude_code_prefix_routes_to_claude_with_prefix_stripped(self):
        r = run_agent(**_kwargs(agent="brainstorm", model="claude-code/opus"))
        self.assertEqual(r.text, "claude")
        self.assertEqual(len(self.claude_calls), 1)
        self.assertEqual(len(self.oc_calls), 0)
        c = self.claude_calls[0]
        self.assertEqual(c["model"], "opus")  # prefix stripped
        self.assertEqual(c["agent"], "brainstorm")  # agent forwarded unchanged

    def test_claude_code_keeps_remaining_slashes(self):
        run_agent(**_kwargs(model="claude-code/anthropic/claude-opus-4-8"))
        self.assertEqual(self.claude_calls[0]["model"], "anthropic/claude-opus-4-8")

    def test_kwargs_forwarded_unchanged_to_claude(self):
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

    def test_mixed_roles_route_independently_by_model(self):
        # a claude-code planner and an opencode executor in the same run
        run_agent(**_kwargs(agent="brainstorm", model="claude-code/opus"))
        run_agent(**_kwargs(agent="executor", model="lmstudio/qwen3.6-27b-mtp"))
        run_agent(**_kwargs(agent="reviewer", model="claude-code/sonnet"))
        self.assertEqual([c["model"] for c in self.claude_calls], ["opus", "sonnet"])
        self.assertEqual([c["model"] for c in self.oc_calls], ["lmstudio/qwen3.6-27b-mtp"])


class TestMonkeyPatchTarget(unittest.TestCase):
    """The existing suite stubs `opencode.run_agent` directly; that target must
    remain a writable module attribute."""

    def test_run_agent_attribute_is_writable_and_callable(self):
        orig = oc.run_agent
        try:
            oc.run_agent = lambda **kw: _result("patched")
            self.assertEqual(oc.run_agent(**_kwargs()).text, "patched")
        finally:
            oc.run_agent = orig


if __name__ == "__main__":
    unittest.main()
