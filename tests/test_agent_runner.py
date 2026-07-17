"""Runner process logic (director/agent/runner.py), with plan/run stubbed.

Exercises workspace preparation from the captured base commit, the JobContext
handed to planning/execution, resume detection, heartbeats, atomic results,
and stable error codes — without ever calling a model provider.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import gitutil  # noqa: E402
from director.agent import storage  # noqa: E402
from director.agent.runner import run_job_process  # noqa: E402
from director.agent.storage import JobStore  # noqa: E402
from director.plan import READY, PlanProgress, PlanResult  # noqa: E402

OK_RUN = {
    "job_id": "j",
    "done": ["n1"],
    "failed": [],
    "escalated": [],
    "integration_ok": True,
    "cost_total": 0.0,
}


def _plan_result(job_id: str) -> PlanResult:
    return PlanResult(False, READY, job_id, f"director/{job_id}", 1, "", "ok")


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        gitutil.git(["config", "user.email", "t@t"], self.repo)
        gitutil.git(["config", "user.name", "t"], self.repo)
        (self.repo / "README.md").write_text("hello\n")
        gitutil.commit_all("init", self.repo)
        self.head = gitutil.current_commit(self.repo)
        self.store = JobStore(self.home)
        jid = storage.new_job_id()
        self.job = self.store.create_job(
            {
                "schema_version": 1,
                "request_id": None,
                "repo": str(self.repo),
                "task": "build the feature",
                "base_ref": "HEAD",
                "base_commit": self.head,
                "job_id": jid,
                "job_branch": storage.job_branch_for(jid),
                "settings": {"parallel": 2, "max_attempts": 0, "max_cost": 0.0},
                "labels": {},
                "submitted_at": storage.utc_now(),
            }
        )
        self.jid = jid

    def _patched(self, plan_fn=None, run_fn=None):
        plan_fn = plan_fn or mock.Mock(return_value=_plan_result(self.jid))
        run_fn = run_fn or mock.Mock(return_value=dict(OK_RUN))
        cfg = mock.Mock()
        cfg.max_attempts = 3
        return (
            mock.patch("director.plan.run_plan", plan_fn),
            mock.patch("director.run.run_job", run_fn),
            mock.patch("director.config.load", mock.Mock(return_value=cfg)),
            plan_fn,
            run_fn,
        )

    def test_success_path_prepares_workspace_and_writes_result(self):
        p1, p2, p3, plan_fn, run_fn = self._patched()
        with p1, p2, p3:
            rc = run_job_process(self.jid, root=self.home, log=lambda m: None)
        self.assertEqual(rc, 0)

        ws = self.store.workspace_dir(self.jid)
        self.assertTrue((ws / ".git").exists())
        self.assertEqual(gitutil.current_branch(ws), self.job["job_branch"])
        self.assertEqual(gitutil.current_commit(ws), self.head)
        # the branch is visible from the source repository
        self.assertTrue(gitutil.branch_exists(self.job["job_branch"], self.repo))
        # the submitted checkout was never moved
        self.assertEqual(gitutil.current_branch(self.repo), "main")

        job = self.store.load_job(self.jid)
        self.assertEqual(job["state"], storage.SUCCEEDED)
        result = self.store.read_result(self.jid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNotNone(self.store.heartbeat_age(self.jid))

        # planning/execution received the job-scoped context, not repo/.director
        ctx = plan_fn.call_args.kwargs["ctx"]
        self.assertEqual(ctx.artifact_dir, self.store.artifacts_dir(self.jid))
        self.assertEqual(ctx.workspace, ws)
        self.assertEqual(ctx.node_worktree_dir, self.store.worktrees_dir(self.jid))
        self.assertEqual(ctx.job_id, self.jid)
        self.assertEqual(ctx.base_commit, self.head)
        run_ctx = run_fn.call_args.kwargs["ctx"]
        self.assertEqual(run_ctx.artifact_dir, self.store.artifacts_dir(self.jid))

        events = [e["event"] for e in self.store.read_events(self.jid)]
        self.assertIn("planning_ready", events)
        self.assertIn("run_finished", events)

    def test_failed_gates_yield_exit_1_and_error_code(self):
        failing = dict(OK_RUN, failed=["n1"], integration_ok=False, done=[])
        p1, p2, p3, _, _ = self._patched(run_fn=mock.Mock(return_value=failing))
        with p1, p2, p3:
            rc = run_job_process(self.jid, root=self.home, log=lambda m: None)
        self.assertEqual(rc, 1)
        job = self.store.load_job(self.jid)
        self.assertEqual(job["state"], storage.FAILED)
        self.assertEqual(job["reason"], "gates_failed")
        self.assertFalse(self.store.read_result(self.jid)["ok"])

    def test_planning_failure_has_stable_code(self):
        p1, p2, p3, _, _ = self._patched(
            plan_fn=mock.Mock(side_effect=RuntimeError("planner exploded"))
        )
        with p1, p2, p3:
            rc = run_job_process(self.jid, root=self.home, log=lambda m: None)
        self.assertEqual(rc, 1)
        result = self.store.read_result(self.jid)
        self.assertEqual(result["error"]["code"], "planning_failed")
        self.assertIn("planner exploded", result["error"]["message"])

    def test_ready_plan_resumes_without_replanning(self):
        art = self.store.artifacts_dir(self.jid)
        PlanProgress(
            job_id=self.jid,
            task="build the feature",
            job_branch=self.job["job_branch"],
            stage=READY,
            auto=True,
            critique=True,
        ).save(art)
        (art / "plan.json").write_text('{"nodes": []}')
        p1, p2, p3, plan_fn, run_fn = self._patched()
        with p1, p2, p3:
            rc = run_job_process(self.jid, root=self.home, log=lambda m: None)
        self.assertEqual(rc, 0)
        plan_fn.assert_not_called()
        run_fn.assert_called_once()

    def test_missing_source_repo_is_invalid_repo(self):
        # point the job at a path that never existed (rmtree of a git repo is
        # unreliable on Windows: read-only object files)
        self.store.update_job(self.jid, repo=str(self.repo / "gone"))
        p1, p2, p3, _, _ = self._patched()
        with p1, p2, p3:
            rc = run_job_process(self.jid, root=self.home, log=lambda m: None)
        self.assertEqual(rc, 1)
        result = self.store.read_result(self.jid)
        self.assertEqual(result["error"]["code"], "invalid_repo")
        self.assertEqual(self.store.load_job(self.jid)["state"], storage.FAILED)

    def test_unexpected_exception_still_leaves_a_result(self):
        p1, p2, p3, _, _ = self._patched(plan_fn=mock.Mock(side_effect=ZeroDivisionError("boom")))
        with p1, p2, p3:
            rc = run_job_process(self.jid, root=self.home, log=lambda m: None)
        self.assertEqual(rc, 1)
        result = self.store.read_result(self.jid)
        self.assertEqual(result["error"]["code"], "internal")
        self.assertIn("ZeroDivisionError", result["error"]["message"])

    def test_over_budget_before_run(self):
        self.store.update_job(self.jid, settings={"parallel": 1, "max_cost": 0.5})
        art = self.store.artifacts_dir(self.jid)
        (art / "costs.jsonl").write_text(
            '{"role": "planner", "model": "m", "input": 1, "output": 1, "cost": 2.5, "node": null, "ts": 0}\n'
        )
        p1, p2, p3, _, run_fn = self._patched()
        with p1, p2, p3:
            rc = run_job_process(self.jid, root=self.home, log=lambda m: None)
        self.assertEqual(rc, 1)
        self.assertEqual(self.store.read_result(self.jid)["error"]["code"], "over_budget")
        run_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
