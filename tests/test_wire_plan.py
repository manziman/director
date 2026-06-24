"""Acceptance tests for the `wire-plan` node.

`director/plan.py` must:
  (1) Import the `opencode` module itself (in addition to the existing
      `from director.opencode import run_agent`) so that
      `opencode.set_runtime` is reachable as a module-attribute call.
  (2) Inside `run_plan`, near the top of the function body (before any
      `run_agent` / `_recon` / `_brainstorm` / `_decompose` helper runs),
      invoke:
          opencode.set_runtime(dict(cfg.runtime))
      passing a copy of the config's role->backend map.

These tests stub out all I/O (git, filesystem, run_agent) so no real
subprocess, network, or model is invoked.

Run: python3 -m unittest discover -s tests -p test_wire_plan.py -q
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import contextlib

import director.plan as plan_mod  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(runtime: dict | None = None):
    """Build a minimal Config-like object with a .runtime attribute."""
    from director import config as config_mod

    rt = runtime if runtime is not None else {}
    try:
        from director.config import Config

        cfg = Config(
            path=Path("<test>"),
            tiers=dict.fromkeys(config_mod.ROLES, "p/m1"),
            gates={},
            pricing={"p/m1": {"input": 0.0, "output": 0.0}},
            limits={"node_timeout_secs": 60, "max_attempts": 1, "flake_runs": 1},
            sampling={},
            local={},
            review={"stage_two": False},
            runtime=rt,
        )
    except TypeError:
        # runtime field not yet on Config — build a plain namespace so the
        # test can still exercise run_plan's call site.
        cfg = types.SimpleNamespace(
            path=Path("<test>"),
            tiers=dict.fromkeys(config_mod.ROLES, "p/m1"),
            gates={},
            pricing={"p/m1": {"input": 0.0, "output": 0.0}},
            limits={"node_timeout_secs": 60, "max_attempts": 1, "flake_runs": 1},
            sampling={},
            local={},
            review={"stage_two": False},
            runtime=rt,
            model_for=lambda role: "p/m1",
            node_timeout=60,
            cost_ceiling=0.0,
            max_attempts=1,
            flake_runs=1,
            stage_two_enabled=False,
            stage_two_file_threshold=3,
            stage_one_llm=False,
            price=lambda model: {"input": 0.0, "output": 0.0},
        )
    return cfg


def _init_git_repo(d: Path, branch: str = "main"):
    """Create a minimal git repo with the given branch checked out."""

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=str(d),
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (d / "README").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    if branch != "main" and branch != "master":
        git("checkout", "-qb", branch)


def _fake_run_result(text: str = ""):
    """Return a minimal successful RunResult."""
    from director.opencode import RunResult

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


def _fake_plan_result():
    """Return a minimal PlanResult-like object."""
    from director.plan import PlanResult

    return PlanResult(
        paused=False,
        stage="ready",
        job_id="test-job",
        job_branch="director/job-test-job",
        n_nodes=0,
        artifact="plan.json",
        message="done",
    )


class _RunPlanHarness(unittest.TestCase):
    """Base class that sets up a minimal git repo and provides a helper to
    run run_plan with all external I/O stubbed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wire-plan-"))
        _init_git_repo(self.tmp)
        # Create .director directory
        fdir = self.tmp / ".director"
        fdir.mkdir(parents=True, exist_ok=True)

    def _run_with_recording(self, runtime: dict, task: str = "test task"):
        """Run run_plan with all external I/O stubbed.

        Returns (set_runtime_calls, run_agent_calls, call_order) where
        call_order is a list of ("set_runtime"|"run_agent", ...) tuples
        in the order they were invoked.

        Strategy: patch `_recon`, `_stage_a_spec`, `_stage_bc_decompose`,
        and all git/setup helpers so run_plan can complete cleanly in --auto
        mode without touching the filesystem or network.
        """
        importlib.reload(plan_mod)
        cfg = _make_config(runtime=runtime)
        set_runtime_calls = []
        run_agent_calls = []
        call_order = []

        oc_in_plan = getattr(plan_mod, "opencode", None)
        orig_sr = None
        if oc_in_plan is not None:
            orig_sr = getattr(oc_in_plan, "set_runtime", None)

        def recording_set_runtime(mapping):
            set_runtime_calls.append(mapping)
            call_order.append(("set_runtime", mapping))

        def recording_run_agent(**kwargs):
            run_agent_calls.append(kwargs)
            call_order.append(("run_agent", kwargs.get("agent")))
            return _fake_run_result(text='{"nodes": []}')

        if oc_in_plan is not None:
            oc_in_plan.set_runtime = recording_set_runtime

        # Build a fake Plan object for _stage_bc_decompose to return
        from director.models import Plan

        fake_plan = Plan(
            job_id="test-job",
            task=task,
            repo=str(self.tmp),
            created_at="2026-01-01T00:00:00",
            job_branch="director/job-test-job",
            nodes=[],
        )

        # Build a fake PlanProgress for the continuation path
        from director.plan import READY, PlanProgress

        PlanProgress(
            job_id="test-job",
            task=task,
            job_branch="director/job-test-job",
            stage=READY,
            auto=True,
            critique=False,
        )

        mock_gitutil = MagicMock()
        mock_gitutil.current_branch.return_value = "main"
        mock_gitutil.branch_exists.return_value = False
        mock_gitutil.current_commit.return_value = "abc123"
        mock_gitutil.git.return_value = MagicMock(returncode=0)
        mock_gitutil.create_branch.return_value = None
        mock_gitutil.checkout.return_value = None
        mock_gitutil.commit_all.return_value = None

        # Fake recon writes recon.md and records a run_agent call
        def fake_recon(prog, repo, cfg, ledger, logs, log):
            call_order.append(("run_agent", "explorer"))
            run_agent_calls.append({"agent": "explorer"})
            (repo / ".director" / "recon.md").write_text("recon summary\n")
            return "recon summary"

        # Fake _stage_a_spec writes spec.md and records a run_agent call
        def fake_stage_a_spec(prog, repo, cfg, summary, ledger, logs, log):
            call_order.append(("run_agent", "brainstorm"))
            run_agent_calls.append({"agent": "brainstorm"})
            (repo / ".director" / "spec.md").write_text("spec content\n")

        # Fake _stage_bc_decompose writes plan.json and records a run_agent call
        def fake_stage_bc_decompose(prog, repo, cfg, ledger, logs, log):
            call_order.append(("run_agent", "planner"))
            run_agent_calls.append({"agent": "planner"})
            plan_json = json.dumps(
                {
                    "job_id": prog.job_id,
                    "task": prog.task,
                    "repo": str(repo),
                    "created_at": "2026-01-01T00:00:00",
                    "job_branch": prog.job_branch,
                    "nodes": [],
                }
            )
            (repo / ".director" / "plan.json").write_text(plan_json)
            return fake_plan

        try:
            with (
                patch.object(plan_mod, "gitutil", mock_gitutil),
                patch.object(plan_mod, "setup") as mock_setup,
                patch.object(plan_mod, "sync_agents", return_value=None),
                patch.object(plan_mod, "_recon", fake_recon),
                patch.object(plan_mod, "_stage_a_spec", fake_stage_a_spec),
                patch.object(plan_mod, "_stage_bc_decompose", fake_stage_bc_decompose),
                patch.object(plan_mod, "_critique_spec", return_value=None),
                patch.object(plan_mod, "_critique_plan", return_value=fake_plan),
            ):
                mock_setup.ensure_director_gitignore = MagicMock()
                plan_mod.run_plan(
                    task=task,
                    repo=str(self.tmp),
                    cfg=cfg,
                    log=lambda *a: None,
                    auto=True,
                    critique=False,
                    cont=False,
                )
        finally:
            if oc_in_plan is not None:
                if orig_sr is not None:
                    oc_in_plan.set_runtime = orig_sr
                else:
                    with contextlib.suppress(AttributeError):
                        del oc_in_plan.set_runtime

        return set_runtime_calls, run_agent_calls, call_order


# ---------------------------------------------------------------------------
# 1. Static import check: `director.plan` must import the `opencode` MODULE
# ---------------------------------------------------------------------------


class TestPlanModuleImportsOpencode(unittest.TestCase):
    """director.plan must import the opencode module (not just symbols from it)."""

    def test_opencode_module_is_imported_in_plan(self):
        """After importing director.plan, `director.opencode` must be in its
        global namespace as the module object (not just run_agent)."""
        importlib.reload(plan_mod)
        plan_globals = vars(plan_mod)
        self.assertIn(
            "opencode",
            plan_globals,
            "director.plan must have 'opencode' in its globals "
            "(add `from director import opencode`)",
        )
        import director.opencode as expected_mod

        self.assertIs(
            plan_globals["opencode"],
            expected_mod,
            "plan_mod.opencode must be the director.opencode module object",
        )

    def test_run_agent_still_importable_from_plan(self):
        """The existing `from director.opencode import run_agent` must still
        be present (the new import is additive)."""
        importlib.reload(plan_mod)
        plan_globals = vars(plan_mod)
        self.assertIn(
            "run_agent",
            plan_globals,
            "run_agent must still be in director.plan's globals",
        )

    def test_opencode_set_runtime_reachable_via_plan_module(self):
        """opencode.set_runtime must be reachable as plan_mod.opencode.set_runtime."""
        importlib.reload(plan_mod)
        oc_in_plan = getattr(plan_mod, "opencode", None)
        self.assertIsNotNone(oc_in_plan, "plan_mod.opencode must exist")
        self.assertTrue(
            callable(getattr(oc_in_plan, "set_runtime", None)),
            "plan_mod.opencode.set_runtime must be callable",
        )


# ---------------------------------------------------------------------------
# 2. run_plan calls opencode.set_runtime(dict(cfg.runtime)) before run_agent
# ---------------------------------------------------------------------------


class TestRunPlanCallsSetRuntime(_RunPlanHarness):
    """run_plan must call opencode.set_runtime(dict(cfg.runtime)) before any
    run_agent invocation."""

    def test_set_runtime_called_once_per_run_plan(self):
        """run_plan must call opencode.set_runtime exactly once."""
        calls, _, _ = self._run_with_recording(runtime={})
        self.assertEqual(
            len(calls),
            1,
            f"expected exactly 1 call to opencode.set_runtime, got {len(calls)}",
        )

    def test_set_runtime_called_with_empty_dict_when_runtime_empty(self):
        """With cfg.runtime == {}, set_runtime must be called with {}."""
        calls, _, _ = self._run_with_recording(runtime={})
        self.assertGreaterEqual(
            len(calls),
            1,
            "set_runtime must be called at least once (was never called)",
        )
        self.assertEqual(
            calls[0],
            {},
            "set_runtime must be called with {} when cfg.runtime is empty",
        )

    def test_set_runtime_called_with_copy_of_runtime(self):
        """set_runtime must receive dict(cfg.runtime) — a dict copy."""
        rt = {"planner": "claude-code", "executor": "opencode"}
        calls, _, _ = self._run_with_recording(runtime=rt)
        self.assertGreaterEqual(
            len(calls),
            1,
            "set_runtime must be called at least once (was never called)",
        )
        self.assertEqual(
            calls[0],
            rt,
            "set_runtime must be called with the contents of cfg.runtime",
        )

    def test_set_runtime_receives_a_dict(self):
        """The argument passed to set_runtime must be a plain dict."""
        calls, _, _ = self._run_with_recording(runtime={"planner": "claude-code"})
        self.assertGreaterEqual(
            len(calls),
            1,
            "set_runtime must be called at least once (was never called)",
        )
        self.assertIsInstance(
            calls[0],
            dict,
            "set_runtime argument must be a plain dict",
        )

    def test_set_runtime_called_before_run_agent(self):
        """set_runtime must be called BEFORE any run_agent invocation."""
        _, _, call_order = self._run_with_recording(runtime={"planner": "claude-code"})

        set_runtime_indices = [i for i, (name, _) in enumerate(call_order) if name == "set_runtime"]
        run_agent_indices = [i for i, (name, _) in enumerate(call_order) if name == "run_agent"]

        self.assertTrue(
            len(set_runtime_indices) >= 1,
            f"set_runtime was never called; call_order={call_order}",
        )
        if run_agent_indices:
            first_set_runtime = set_runtime_indices[0]
            first_run_agent = run_agent_indices[0]
            self.assertLess(
                first_set_runtime,
                first_run_agent,
                f"set_runtime (pos {first_set_runtime}) must be called BEFORE "
                f"run_agent (pos {first_run_agent}); call_order={call_order}",
            )

    def test_set_runtime_called_before_recon(self):
        """set_runtime must be called BEFORE the _recon helper (which calls run_agent)."""
        _, _, call_order = self._run_with_recording(runtime={})

        set_runtime_indices = [i for i, (name, _) in enumerate(call_order) if name == "set_runtime"]
        recon_indices = [
            i
            for i, (name, agent) in enumerate(call_order)
            if name == "run_agent" and agent == "explorer"
        ]

        self.assertTrue(
            len(set_runtime_indices) >= 1,
            f"set_runtime was never called; call_order={call_order}",
        )
        if recon_indices:
            first_set_runtime = set_runtime_indices[0]
            first_recon = recon_indices[0]
            self.assertLess(
                first_set_runtime,
                first_recon,
                f"set_runtime (pos {first_set_runtime}) must be called BEFORE "
                f"_recon/explorer (pos {first_recon}); call_order={call_order}",
            )


# ---------------------------------------------------------------------------
# 3. The call is a no-op with empty runtime (does not break existing behavior)
# ---------------------------------------------------------------------------


class TestSetRuntimeNoOpWithEmptyRuntime(_RunPlanHarness):
    """With cfg.runtime == {}, the set_runtime call is harmless."""

    def test_run_plan_completes_without_error_when_runtime_empty(self):
        """run_plan must not raise when cfg.runtime == {} (the default case)."""
        try:
            self._run_with_recording(runtime={})
        except Exception as exc:
            self.fail(f"run_plan raised {type(exc).__name__}: {exc} when cfg.runtime == {{}}")

    def test_run_plan_returns_plan_result_when_runtime_empty(self):
        """run_plan must return a PlanResult (not None) when runtime is empty."""
        importlib.reload(plan_mod)
        cfg = _make_config(runtime={})

        oc_in_plan = getattr(plan_mod, "opencode", None)
        orig_sr = None
        if oc_in_plan is not None:
            orig_sr = getattr(oc_in_plan, "set_runtime", None)
            oc_in_plan.set_runtime = lambda m: None

        from director.models import Plan

        fake_plan = Plan(
            job_id="test-job",
            task="test task",
            repo=str(self.tmp),
            created_at="2026-01-01T00:00:00",
            job_branch="director/job-test-job",
            nodes=[],
        )

        def fake_recon(prog, repo, cfg, ledger, logs, log):
            (repo / ".director" / "recon.md").write_text("recon summary\n")
            return "recon summary"

        def fake_stage_a_spec(prog, repo, cfg, summary, ledger, logs, log):
            (repo / ".director" / "spec.md").write_text("spec content\n")

        def fake_stage_bc_decompose(prog, repo, cfg, ledger, logs, log):
            plan_json = json.dumps(
                {
                    "job_id": prog.job_id,
                    "task": prog.task,
                    "repo": str(repo),
                    "created_at": "2026-01-01T00:00:00",
                    "job_branch": prog.job_branch,
                    "nodes": [],
                }
            )
            (repo / ".director" / "plan.json").write_text(plan_json)
            return fake_plan

        mock_gitutil = MagicMock()
        mock_gitutil.current_branch.return_value = "main"
        mock_gitutil.branch_exists.return_value = False
        mock_gitutil.current_commit.return_value = "abc123"
        mock_gitutil.git.return_value = MagicMock(returncode=0)
        mock_gitutil.create_branch.return_value = None
        mock_gitutil.checkout.return_value = None
        mock_gitutil.commit_all.return_value = None

        try:
            with (
                patch.object(plan_mod, "gitutil", mock_gitutil),
                patch.object(plan_mod, "setup") as mock_setup,
                patch.object(plan_mod, "sync_agents", return_value=None),
                patch.object(plan_mod, "_recon", fake_recon),
                patch.object(plan_mod, "_stage_a_spec", fake_stage_a_spec),
                patch.object(plan_mod, "_stage_bc_decompose", fake_stage_bc_decompose),
                patch.object(plan_mod, "_critique_spec", return_value=None),
                patch.object(plan_mod, "_critique_plan", return_value=fake_plan),
            ):
                mock_setup.ensure_director_gitignore = MagicMock()
                result = plan_mod.run_plan(
                    task="test task",
                    repo=str(self.tmp),
                    cfg=cfg,
                    log=lambda *a: None,
                    auto=True,
                    critique=False,
                    cont=False,
                )
        finally:
            if oc_in_plan is not None:
                if orig_sr is not None:
                    oc_in_plan.set_runtime = orig_sr
                else:
                    with contextlib.suppress(AttributeError):
                        del oc_in_plan.set_runtime

        self.assertIsNotNone(result, "run_plan must return a PlanResult, not None")
        from director.plan import PlanResult

        self.assertIsInstance(result, PlanResult, "run_plan must return a PlanResult instance")


# ---------------------------------------------------------------------------
# 4. set_runtime receives a COPY (dict(cfg.runtime)), not the original object
# ---------------------------------------------------------------------------


class TestSetRuntimeReceivesCopy(_RunPlanHarness):
    """The argument to set_runtime must be dict(cfg.runtime), not cfg.runtime itself."""

    def test_set_runtime_arg_equals_cfg_runtime_contents(self):
        """dict(cfg.runtime) creates a new dict equal to cfg.runtime."""
        rt = {"planner": "claude-code"}
        calls, _, _ = self._run_with_recording(runtime=rt)
        self.assertEqual(len(calls), 1, "set_runtime must be called once")
        self.assertEqual(
            calls[0],
            rt,
            "set_runtime arg must equal cfg.runtime contents",
        )

    def test_set_runtime_arg_is_a_plain_dict(self):
        """dict(cfg.runtime) always produces a plain dict."""
        rt = {"planner": "claude-code"}
        calls, _, _ = self._run_with_recording(runtime=rt)
        self.assertGreaterEqual(
            len(calls),
            1,
            "set_runtime must be called at least once",
        )
        self.assertIsInstance(
            calls[0],
            dict,
            "set_runtime arg must be a plain dict",
        )

    def test_set_runtime_called_with_multiple_roles(self):
        """All roles in cfg.runtime are forwarded to set_runtime."""
        rt = {"planner": "claude-code", "executor": "opencode", "reviewer": "claude-code"}
        calls, _, _ = self._run_with_recording(runtime=rt)
        self.assertGreaterEqual(
            len(calls),
            1,
            "set_runtime must be called at least once",
        )
        self.assertEqual(calls[0], rt)


# ---------------------------------------------------------------------------
# 5. Placement: set_runtime is called right after repo = Path(repo).resolve()
#    and before any helper that invokes run_agent
# ---------------------------------------------------------------------------


class TestSetRuntimePlacement(_RunPlanHarness):
    """set_runtime must be called near the top of run_plan, before any agent."""

    def test_set_runtime_is_first_call_in_call_order(self):
        """set_runtime must appear as the very first recorded call in call_order
        (before any run_agent / _recon / _brainstorm / _decompose)."""
        _, _, call_order = self._run_with_recording(runtime={})
        self.assertTrue(
            len(call_order) >= 1,
            "call_order is empty; expected at least set_runtime to appear",
        )
        first_name, _ = call_order[0]
        self.assertEqual(
            first_name,
            "set_runtime",
            f"The first call in call_order must be 'set_runtime', "
            f"but got '{first_name}'; full call_order={call_order}",
        )

    def test_set_runtime_called_before_brainstorm(self):
        """set_runtime must be called BEFORE the brainstorm agent."""
        _, _, call_order = self._run_with_recording(runtime={})

        set_runtime_indices = [i for i, (name, _) in enumerate(call_order) if name == "set_runtime"]
        brainstorm_indices = [
            i
            for i, (name, agent) in enumerate(call_order)
            if name == "run_agent" and agent == "brainstorm"
        ]

        self.assertTrue(
            len(set_runtime_indices) >= 1,
            f"set_runtime was never called; call_order={call_order}",
        )
        if brainstorm_indices:
            self.assertLess(
                set_runtime_indices[0],
                brainstorm_indices[0],
                f"set_runtime must precede brainstorm; call_order={call_order}",
            )

    def test_set_runtime_called_before_planner_decompose(self):
        """set_runtime must be called BEFORE the planner/decompose agent."""
        _, _, call_order = self._run_with_recording(runtime={})

        set_runtime_indices = [i for i, (name, _) in enumerate(call_order) if name == "set_runtime"]
        planner_indices = [
            i
            for i, (name, agent) in enumerate(call_order)
            if name == "run_agent" and agent == "planner"
        ]

        self.assertTrue(
            len(set_runtime_indices) >= 1,
            f"set_runtime was never called; call_order={call_order}",
        )
        if planner_indices:
            self.assertLess(
                set_runtime_indices[0],
                planner_indices[0],
                f"set_runtime must precede planner/decompose; call_order={call_order}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
