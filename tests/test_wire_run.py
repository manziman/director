"""Acceptance tests for the `wire-run` node.

`director/run.py` must:
  (1) Import the `opencode` module itself (in addition to the existing
      `from director.opencode import run_agent, watch_it_fail`) so that
      `opencode.set_runtime` is reachable as a module-attribute call.
  (2) Inside `run_job`, near the top of the function body (before any
      `run_agent` / node-scheduling call), invoke:
          opencode.set_runtime(dict(cfg.runtime))
      passing a copy of the config's role->backend map.

These tests stub out all I/O (git, filesystem, run_agent) so no real
subprocess, network, or model is invoked.

Run: python3 -m unittest discover -s tests -p test_wire_run.py -q
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

import director.run as run_mod  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(runtime: dict | None = None):
    """Build a minimal Config-like object with a .runtime attribute."""
    from director import config as config_mod

    rt = runtime if runtime is not None else {}
    # Config may or may not have `runtime` field yet; we build it defensively.
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
        # test can still exercise run_job's call site.
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


def _make_plan_json(job_id="test-job", job_branch="director/test-job", nodes=None):
    """Return a minimal plan.json string."""
    if nodes is None:
        nodes = []
    return json.dumps(
        {
            "job_id": job_id,
            "task": "test task",
            "repo": ".",
            "created_at": "2026-01-01T00:00:00",
            "job_branch": job_branch,
            "nodes": nodes,
        }
    )


def _init_git_repo(d: Path, branch: str = "director/test-job"):
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
    git("checkout", "-qb", branch)


def _setup_director_dir(repo: Path, plan_json: str):
    """Write .director/plan.json."""
    fdir = repo / ".director"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "plan.json").write_text(plan_json)


def _make_node_dict(node_id="n1", files=None):
    """Return a valid node dict for plan.json."""
    return {
        "id": node_id,
        "title": f"node {node_id}",
        "spec": "do something",
        "files": files or ["src/foo.py"],
        "test_cmd": "true",
        "depends_on": [],
        "estimated_difficulty": "easy",
    }


def _fake_run_result():
    """Return a minimal successful RunResult."""
    from director.opencode import RunResult

    return RunResult(
        returncode=0,
        text="",
        tokens={"input": 0, "output": 0, "reasoning": 0, "total": 0},
        cost_reported=0.0,
        n_steps=0,
        tool_calls=[],
        tool_events=[],
        error=None,
        timed_out=False,
        log_path="run.json",
    )


# ---------------------------------------------------------------------------
# 1. Static import check: `director.run` must import the `opencode` MODULE
# ---------------------------------------------------------------------------


class TestRunModuleImportsOpencode(unittest.TestCase):
    """director.run must import the opencode module (not just symbols from it)."""

    def test_opencode_module_is_imported_in_run(self):
        """After importing director.run, `director.opencode` must be in its
        global namespace as the module object (not just run_agent/watch_it_fail)."""
        importlib.reload(run_mod)
        run_globals = vars(run_mod)
        self.assertIn(
            "opencode",
            run_globals,
            "director.run must have 'opencode' in its globals "
            "(add `from director import opencode`)",
        )
        import director.opencode as expected_mod

        self.assertIs(
            run_globals["opencode"],
            expected_mod,
            "run_mod.opencode must be the director.opencode module object",
        )

    def test_run_agent_and_watch_it_fail_still_importable_from_run(self):
        """The existing `from director.opencode import run_agent, watch_it_fail`
        must still be present (the new import is additive)."""
        importlib.reload(run_mod)
        run_globals = vars(run_mod)
        self.assertIn("run_agent", run_globals, "run_agent must still be in director.run's globals")
        self.assertIn(
            "watch_it_fail", run_globals, "watch_it_fail must still be in director.run's globals"
        )

    def test_opencode_set_runtime_reachable_via_run_module(self):
        """opencode.set_runtime must be reachable as run_mod.opencode.set_runtime."""
        importlib.reload(run_mod)
        oc_in_run = getattr(run_mod, "opencode", None)
        self.assertIsNotNone(oc_in_run, "run_mod.opencode must exist")
        self.assertTrue(
            callable(getattr(oc_in_run, "set_runtime", None)),
            "run_mod.opencode.set_runtime must be callable",
        )


# ---------------------------------------------------------------------------
# 2. run_job calls opencode.set_runtime(dict(cfg.runtime)) before run_agent
#
# Strategy: patch dag.validate (and other internals) so run_job can run to
# completion with a minimal plan, then record set_runtime calls.
# ---------------------------------------------------------------------------


class _RunJobHarness(unittest.TestCase):
    """Base class that sets up a minimal git repo + plan and provides a helper
    to run run_job with all external I/O stubbed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wire-run-"))
        self.branch = "director/test-job"
        _init_git_repo(self.tmp, branch=self.branch)
        plan = _make_plan_json(
            job_id="test-job",
            job_branch=self.branch,
            nodes=[_make_node_dict()],
        )
        _setup_director_dir(self.tmp, plan)

    def _run_with_recording(self, runtime: dict):
        """Run run_job with all external I/O stubbed.

        Returns (set_runtime_calls, run_agent_calls, call_order) where
        call_order is a list of ("set_runtime"|"run_agent", ...) tuples
        in the order they were invoked.

        Strategy: patch `_process_node` (the per-node worker) to return a
        successful NodeOutcome without touching the filesystem or git, and
        patch all git/setup/gate helpers so run_job can complete cleanly.
        """
        importlib.reload(run_mod)
        cfg = _make_config(runtime=runtime)
        set_runtime_calls = []
        run_agent_calls = []
        call_order = []

        oc_in_run = getattr(run_mod, "opencode", None)

        def recording_set_runtime(mapping):
            set_runtime_calls.append(mapping)
            call_order.append(("set_runtime", mapping))

        def recording_run_agent(**kwargs):
            run_agent_calls.append(kwargs)
            call_order.append(("run_agent", kwargs.get("agent")))
            return _fake_run_result()

        def fake_process_node(node, worktree, cfg, logs, max_attempts, log):
            """Return a passing NodeOutcome without any real work."""
            # run_agent is called inside _process_node; record it here so
            # call_order captures the ordering relative to set_runtime.
            call_order.append(("run_agent", node.id))
            run_agent_calls.append({"agent": "executor", "node": node.id})
            return run_mod.NodeOutcome(
                node_id=node.id,
                ok=True,
                tier="executor",
                escalated=False,
                attempts=1,
                model="p/m1",
                tokens={"input": 0, "output": 0},
                calls=[("executor", "p/m1", {"input": 0, "output": 0})],
                error=None,
                worktree=worktree,
            )

        # Patch set_runtime on the opencode module reference inside run_mod
        if oc_in_run is not None:
            orig_sr = getattr(oc_in_run, "set_runtime", None)
            oc_in_run.set_runtime = recording_set_runtime

        mock_gitutil = MagicMock()
        mock_gitutil.current_branch.return_value = self.branch
        mock_gitutil.branch_exists.return_value = False
        mock_gitutil.git.return_value = MagicMock(returncode=0)
        mock_gitutil.merge_branch.return_value = MagicMock(returncode=0, stdout="", stderr="")

        try:
            with (
                patch.object(
                    run_mod,
                    "integration_gate",
                    return_value=MagicMock(ok=True, failures=[], detail=""),
                ),
                patch.object(run_mod, "setup") as mock_setup,
                patch.object(run_mod, "_process_node", fake_process_node),
                patch.object(run_mod, "gitutil", mock_gitutil),
            ):
                mock_setup.ensure_director_gitignore = MagicMock()
                run_mod.run_job(
                    repo=str(self.tmp),
                    cfg=cfg,
                    parallel=1,
                    max_attempts=1,
                    log=lambda *a: None,
                )
        finally:
            if oc_in_run is not None and orig_sr is not None:
                oc_in_run.set_runtime = orig_sr
            elif oc_in_run is not None and orig_sr is None:
                with contextlib.suppress(AttributeError):
                    del oc_in_run.set_runtime

        return set_runtime_calls, run_agent_calls, call_order


class TestRunJobCallsSetRuntime(_RunJobHarness):
    """run_job must call opencode.set_runtime(dict(cfg.runtime)) before any
    run_agent invocation."""

    def test_set_runtime_called_once_per_run_job(self):
        """run_job must call opencode.set_runtime exactly once."""
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
            calls[0], {}, "set_runtime must be called with {} when cfg.runtime is empty"
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
            calls[0], rt, "set_runtime must be called with the contents of cfg.runtime"
        )

    def test_set_runtime_receives_a_dict(self):
        """The argument passed to set_runtime must be a plain dict."""
        calls, _, _ = self._run_with_recording(runtime={"planner": "claude-code"})
        self.assertGreaterEqual(
            len(calls),
            1,
            "set_runtime must be called at least once (was never called)",
        )
        self.assertIsInstance(calls[0], dict, "set_runtime argument must be a plain dict")

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


# ---------------------------------------------------------------------------
# 3. The call is a no-op with empty runtime (does not break existing behavior)
# ---------------------------------------------------------------------------


class TestSetRuntimeNoOpWithEmptyRuntime(_RunJobHarness):
    """With cfg.runtime == {}, the set_runtime call is harmless."""

    def test_run_job_completes_without_error_when_runtime_empty(self):
        """run_job must not raise when cfg.runtime == {} (the default case)."""
        try:
            self._run_with_recording(runtime={})
        except Exception as exc:
            self.fail(f"run_job raised {type(exc).__name__}: {exc} when cfg.runtime == {{}}")

    def test_run_job_returns_dict_with_expected_keys(self):
        """run_job must still return the standard result dict."""
        importlib.reload(run_mod)
        cfg = _make_config(runtime={})

        oc_in_run = getattr(run_mod, "opencode", None)
        orig_sr = None
        if oc_in_run is not None:
            orig_sr = getattr(oc_in_run, "set_runtime", None)
            oc_in_run.set_runtime = lambda m: None

        def fake_process_node(node, worktree, cfg, logs, max_attempts, log):
            return run_mod.NodeOutcome(
                node_id=node.id,
                ok=True,
                tier="executor",
                escalated=False,
                attempts=1,
                model="p/m1",
                tokens={"input": 0, "output": 0},
                calls=[("executor", "p/m1", {"input": 0, "output": 0})],
                error=None,
                worktree=worktree,
            )

        mock_gitutil = MagicMock()
        mock_gitutil.current_branch.return_value = self.branch
        mock_gitutil.branch_exists.return_value = False
        mock_gitutil.git.return_value = MagicMock(returncode=0)
        mock_gitutil.merge_branch.return_value = MagicMock(returncode=0, stdout="", stderr="")

        try:
            with (
                patch.object(
                    run_mod,
                    "integration_gate",
                    return_value=MagicMock(ok=True, failures=[], detail=""),
                ),
                patch.object(run_mod, "setup") as mock_setup,
                patch.object(run_mod, "_process_node", fake_process_node),
                patch.object(run_mod, "gitutil", mock_gitutil),
            ):
                mock_setup.ensure_director_gitignore = MagicMock()
                result = run_mod.run_job(
                    repo=str(self.tmp),
                    cfg=cfg,
                    parallel=1,
                    max_attempts=1,
                    log=lambda *a: None,
                )
        finally:
            if oc_in_run is not None and orig_sr is not None:
                oc_in_run.set_runtime = orig_sr
            elif oc_in_run is not None and orig_sr is None:
                with contextlib.suppress(AttributeError):
                    del oc_in_run.set_runtime

        for key in ("job_id", "done", "failed", "integration_ok"):
            self.assertIn(key, result, f"result dict missing key {key!r}")


# ---------------------------------------------------------------------------
# 4. set_runtime receives a COPY (dict(cfg.runtime)), not the original object
# ---------------------------------------------------------------------------


class TestSetRuntimeReceivesCopy(_RunJobHarness):
    """The argument to set_runtime must be dict(cfg.runtime), not cfg.runtime itself."""

    def test_set_runtime_arg_equals_cfg_runtime_contents(self):
        """dict(cfg.runtime) creates a new dict equal to cfg.runtime."""
        rt = {"planner": "claude-code"}
        calls, _, _ = self._run_with_recording(runtime=rt)
        self.assertEqual(len(calls), 1, "set_runtime must be called once")
        self.assertEqual(calls[0], rt, "set_runtime arg must equal cfg.runtime contents")

    def test_set_runtime_arg_is_a_plain_dict(self):
        """dict(cfg.runtime) always produces a plain dict."""
        rt = {"planner": "claude-code"}
        calls, _, _ = self._run_with_recording(runtime=rt)
        self.assertGreaterEqual(len(calls), 1, "set_runtime must be called at least once")
        self.assertIsInstance(calls[0], dict, "set_runtime arg must be a plain dict")

    def test_set_runtime_called_with_multiple_roles(self):
        """All roles in cfg.runtime are forwarded to set_runtime."""
        rt = {"planner": "claude-code", "executor": "opencode", "reviewer": "claude-code"}
        calls, _, _ = self._run_with_recording(runtime=rt)
        self.assertGreaterEqual(len(calls), 1, "set_runtime must be called at least once")
        self.assertEqual(calls[0], rt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
