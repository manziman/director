"""Offline validation suite.

Stubs only the single external boundary (opencode.run_agent); everything else —
gates, the run scheduler, metrics emission, and the bench orchestration — is the
real code, exercised against real temp git repos with stdlib unittest. No model
or network time is spent.

Note on scope: these stubs prove the *control flow* fast and deterministically,
but integration faults (git semantics, the process environment, a target repo's
.gitignore) only surface against real OpenCode + git + a real model. Treat a live
run — wrapped in a wall-clock `timeout` — as mandatory acceptance for anything
touching git, subprocess env, or the filesystem; the stubs cannot model those.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import config, gitutil
from director.config import Config
from director.gates import node_gate
from director.models import Node
from director.opencode import RunResult, watch_it_fail


def git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def init_repo(d: Path):
    git(["init", "-q"], d)
    git(["config", "user.email", "t@t"], d)
    git(["config", "user.name", "t"], d)
    git(["config", "commit.gpgsign", "false"], d)
    (d / "README").write_text("x\n")
    git(["add", "-A"], d)
    git(["commit", "-qm", "init"], d)


def mk_config(d: Path, *, executor="opencode/local/exec", flake_runs=2) -> Config:
    """Construct a Config directly (no toml needed)."""
    return Config(
        path=d / ".director" / "config.toml",
        tiers={
            "planner": "claude-code/cloud/plan",
            "test_author": "claude-code/cloud/plan",
            "executor": executor,
            "explorer": executor,
            "reviewer": "claude-code/cloud/rev",
            "escalation": "claude-code/cloud/esc",
        },
        gates={"test": ""},
        pricing={
            "opencode/local/exec": {"input": 0.0, "output": 0.0},
            "claude-code/cloud/plan": {"input": 3.0, "output": 15.0},
        },
        limits={
            "flake_runs": flake_runs,
            "node_timeout_secs": 60,
            "max_attempts": 2,
            "cost_ceiling_usd": 0.0,
        },
        sampling={},
        review={"stage_two": True, "stage_two_file_threshold": 3, "stage_one_llm": False},
    )


# --------------------------------------------------------------------------- #
class WatchItFailTests(unittest.TestCase):
    def test_test_run_before_edit_is_observed(self):
        ev = [
            {"name": "bash", "blob": "python -m unittest ... failed traceback"},
            {"name": "write", "blob": "wrote mod.py"},
        ]
        r = watch_it_fail(ev, "python -m unittest")
        self.assertEqual(r.verdict, "observed")
        self.assertTrue(r.ran_before_edit)
        self.assertTrue(r.observed_failure)

    def test_edit_before_test_is_not_observed(self):
        ev = [
            {"name": "write", "blob": "wrote mod.py"},
            {"name": "bash", "blob": "python -m unittest ... ok"},
        ]
        r = watch_it_fail(ev, "python -m unittest")
        self.assertEqual(r.verdict, "not_observed")
        self.assertFalse(r.observed)

    def test_no_tools_is_unknown(self):
        r = watch_it_fail([], "pytest")
        self.assertEqual(r.verdict, "unknown")

    def test_matches_distinctive_token_from_test_cmd(self):
        ev = [{"name": "bash", "blob": "running ./run_specs.sh now"}]
        r = watch_it_fail(ev, "bash run_specs.sh")
        self.assertEqual(r.verdict, "observed")


# --------------------------------------------------------------------------- #
class FlakeControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-flake-"))
        init_repo(self.tmp)
        self.counter = self.tmp.parent / (self.tmp.name + ".counter")

    def _toggle_cmd(self):
        # passes the FIRST time it runs, fails every time after (deterministic
        # stand-in for a flaky suite). Counter lives OUTSIDE the worktree so it
        # never trips the allowlist gate.
        p = str(self.counter)
        return (
            f"python3 -c \"import os;p=r'{p}';"
            "n=int(open(p).read()) if os.path.exists(p) else 0;"
            "open(p,'w').write(str(n+1));"
            'raise SystemExit(0 if n==0 else 1)"'
        )

    def test_flaky_node_is_rejected(self):
        cfg = mk_config(self.tmp, flake_runs=2)
        node = Node(
            id="n",
            title="n",
            spec="s",
            files=["s.py"],
            test_cmd=self._toggle_cmd(),
            tests=[],
            test_hashes={},
        )
        g = node_gate(node, self.tmp, cfg)
        self.assertFalse(g.ok)
        self.assertIn("flaky tests", g.failures)

    def test_stable_node_passes_with_flake_check(self):
        cfg = mk_config(self.tmp, flake_runs=2)
        node = Node(
            id="n",
            title="n",
            spec="s",
            files=["s.py"],
            test_cmd='python3 -c "raise SystemExit(0)"',
            tests=[],
            test_hashes={},
        )
        g = node_gate(node, self.tmp, cfg)
        self.assertTrue(g.ok, g.detail)

    def test_flake_disabled_when_runs_is_one(self):
        cfg = mk_config(self.tmp, flake_runs=1)
        node = Node(
            id="n",
            title="n",
            spec="s",
            files=["s.py"],
            test_cmd=self._toggle_cmd(),
            tests=[],
            test_hashes={},
        )
        g = node_gate(node, self.tmp, cfg)  # only one run → the toggling never bites
        self.assertTrue(g.ok, g.detail)


# --------------------------------------------------------------------------- #
class RunMetricsTests(unittest.TestCase):
    """Drive the REAL run_job with a stubbed executor; assert metrics.jsonl."""

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        self.home = Path(tempfile.mkdtemp(prefix="fm-run-home-"))
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-run-"))
        init_repo(self.tmp)
        self.fdir = self.tmp / ".director"
        self.fdir.mkdir()
        (self.tmp / "tests").mkdir()
        # committed failing acceptance test on a job branch
        git(["checkout", "-q", "-b", "director/job-T"], self.tmp)
        test_src = (
            "import mod, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(mod.add(1,2), 3)\n"
        )
        (self.tmp / "tests" / "test_mod.py").write_text(test_src)
        (self.tmp / "tests" / "__init__.py").write_text("")
        git(["add", "-A"], self.tmp)
        git(["commit", "-qm", "tests"], self.tmp)
        import hashlib

        h = hashlib.sha256((self.tmp / "tests" / "test_mod.py").read_bytes()).hexdigest()
        plan = {
            "job_id": "T",
            "task": "add",
            "repo": str(self.tmp),
            "created_at": "now",
            "job_branch": "director/job-T",
            "nodes": [
                {
                    "id": "mod",
                    "title": "mod",
                    "spec": "implement add",
                    "files": ["mod.py"],
                    "depends_on": [],
                    "test_cmd": "python3 -m unittest discover -s tests -q",
                    "tests": ["tests/test_mod.py"],
                    "estimated_difficulty": "easy",
                    "test_hashes": {"tests/test_mod.py": h},
                }
            ],
        }
        (self.fdir / "plan.json").write_text(json.dumps(plan))
        # a config.toml so config.load works
        (self.fdir / "config.toml").write_text(
            '[tiers]\nplanner="claude-code/cloud/plan"\ntest_author="claude-code/cloud/plan"\n'
            'executor="opencode/local/exec"\nexplorer="opencode/local/exec"\n'
            'reviewer="claude-code/cloud/rev"\nescalation="claude-code/cloud/esc"\n'
            '[gates]\ntest="python3 -m unittest discover -s tests -q"\n'
            "[pricing]\n[limits]\nflake_runs=2\nnode_timeout_secs=60\nmax_attempts=2\n"
            "[review]\nstage_two=true\nstage_two_file_threshold=3\n"
        )

    def tearDown(self):
        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_run_emits_metrics_and_records_watch_it_fail(self):
        import director.run as run

        def fake_run_agent(*, agent, model, message, cwd, log_path, timeout):
            # write a correct implementation into the worktree
            (Path(cwd) / "mod.py").write_text("def add(a, b):\n    return a + b\n")
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("{}\n")
            return RunResult(
                returncode=0,
                text="done",
                tokens={"input": 200, "output": 80, "reasoning": 0, "total": 280},
                cost_reported=0.0,
                n_steps=2,
                tool_calls=[("bash", "completed"), ("write", "completed")],
                tool_events=[
                    {"name": "bash", "blob": "python3 -m unittest ... failed (errors=1) traceback"},
                    {"name": "write", "blob": "wrote mod.py def add"},
                ],
                error=None,
                timed_out=False,
                log_path=str(log_path),
            )

        orig = run.run_agent
        run.run_agent = fake_run_agent
        try:
            cfg = config.load(self.tmp)
            result = run.run_job(str(self.tmp), cfg, parallel=1, max_attempts=2, log=lambda m: None)
        finally:
            run.run_agent = orig

        self.assertEqual(result["done"], ["mod"])
        self.assertTrue(result["integration_ok"], result.get("integration_detail"))
        self.assertEqual(result["executor_tier_pct"], 100.0)
        self.assertIn("wall_secs", result)

        recs = [json.loads(ln) for ln in (self.fdir / "metrics.jsonl").read_text().splitlines()]
        nodes = [r for r in recs if r["kind"] == "node"]
        runs = [r for r in recs if r["kind"] == "run"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(nodes[0]["status"], "done")
        self.assertEqual(nodes[0]["watch_it_fail"]["verdict"], "observed")
        self.assertFalse(nodes[0]["flake_failed"])
        self.assertIn("by_role", nodes[0])
        self.assertEqual(runs[0]["executor_tier_completion"], 1)
        self.assertEqual(runs[0]["n_nodes"], 1)
        self.assertGreaterEqual(runs[0]["wall_secs"], 0.0)

        state = json.loads((self.fdir / "state.json").read_text())
        self.assertEqual(state["nodes"]["mod"]["watch_it_fail"], "observed")


# --------------------------------------------------------------------------- #
class BenchOrchestrationTests(unittest.TestCase):
    """Drive run_bench with stubbed plan/run; assert branch+config restore and
    that each profile faces the same frozen plan branch + the report math."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-bench-"))
        init_repo(self.tmp)
        self.fdir = self.tmp / ".director"
        (self.fdir / "profiles").mkdir(parents=True)
        for name in ("all-frontier", "cheap-cloud"):
            (self.fdir / "profiles" / f"{name}.toml").write_text(
                f'[tiers]\nplanner="claude-code/cloud/plan"\n'
                f'test_author="claude-code/cloud/plan"\nexecutor="opencode/x/{name}"\n'
                'explorer="opencode/x/e"\nreviewer="claude-code/cloud/rev"\n'
                'escalation="claude-code/cloud/esc"\n'
                "[limits]\nmax_attempts=2\n[review]\n"
            )
        (self.fdir / "config.toml").write_text("ORIGINAL\n")
        # config + profiles are tracked (committed on the base branch), as in a
        # real target repo — so restoring the base branch restores them.
        git(["add", "-A"], self.tmp)
        git(["commit", "-qm", "director config"], self.tmp)

    def test_bench_plans_once_runs_each_profile_and_restores(self):
        import director.bench as bench

        seen_branches = []

        def fake_run_plan(task, repo, cfg, log, **kw):
            from director.plan import PlanResult

            repo = Path(repo)
            gitutil.git(["checkout", "-q", "-b", "director/job-B"], repo)
            (repo / "t.py").write_text("def test_x():\n    assert False\n")
            gitutil.git(["add", "-A"], repo)
            gitutil.git(["-c", "commit.gpgsign=false", "commit", "-qm", "tests"], repo)
            plan = {
                "job_id": "B",
                "task": task,
                "repo": str(repo),
                "created_at": "now",
                "job_branch": "director/job-B",
                "nodes": [
                    {
                        "id": "n",
                        "title": "n",
                        "spec": "s",
                        "files": ["a.py"],
                        "depends_on": [],
                        "test_cmd": "true",
                        "tests": ["t.py"],
                        "estimated_difficulty": "easy",
                        "test_hashes": {},
                    }
                ],
            }
            (self.fdir / "plan.json").write_text(json.dumps(plan))
            (self.fdir / "costs.jsonl").write_text(
                json.dumps(
                    {
                        "role": "planner",
                        "model": "claude-code/cloud/plan",
                        "input": 1000,
                        "output": 500,
                        "cost": 0.0105,
                        "node": None,
                        "ts": 0.0,
                    }
                )
                + "\n"
            )
            return PlanResult(False, "ready", "B", "director/job-B", 1, "", "ok")

        def fake_run_job(repo, cfg, parallel, max_attempts, log):
            plan = json.loads((self.fdir / "plan.json").read_text())
            seen_branches.append((cfg.model_for("executor"), plan["job_branch"]))
            # the bench harness creates the branch; the real run_job checks it out
            gitutil.checkout(plan["job_branch"], Path(repo))
            cost = 1.00 if "all-frontier" in cfg.model_for("executor") else 0.10
            return {
                "job_id": plan["job_id"],
                "done": ["n"],
                "failed": [],
                "escalated": [],
                "reviewed": [],
                "review_blocked": [],
                "integration_ok": True,
                "integration_detail": "",
                "cost_total": cost,
                "by_role": {},
                "by_model": {},
                "wall_secs": 1.0,
                "n_nodes": 1,
                "executor_tier_completion": 1,
                "executor_tier_pct": 100.0,
                "escalation_rate": 0.0,
                "stage_two_trigger_rate": 0.0,
            }

        o1, o2 = bench.run_plan, bench.run_job
        bench.run_plan, bench.run_job = fake_run_plan, fake_run_job
        try:
            res = bench.run_bench(
                "do x", str(self.tmp), ["all-frontier", "cheap-cloud"], lambda m: None
            )
        finally:
            bench.run_plan, bench.run_job = o1, o2

        # both profiles ran against a branch forked from the frozen plan branch
        self.assertEqual(len(res.rows), 2)
        self.assertTrue(all(r.integration_ok for r in res.rows))
        for _exec, branch in seen_branches:
            self.assertTrue(branch.startswith("director/bench-"))
        # config.toml restored to the original content
        self.assertEqual((self.fdir / "config.toml").read_text(), "ORIGINAL\n")
        # back on the base branch
        self.assertEqual(gitutil.current_branch(self.tmp), "master")
        # summary + per-profile metrics streams copied
        self.assertTrue((self.fdir / "bench" / "summary.json").exists())
        # report math: cheap-cloud is 90% cheaper than all-frontier baseline
        rep = bench.bench_report(res)
        self.assertIn("90% cheaper", rep)
        self.assertIn("shared plan cost $0.0105", rep)


class DirectorGitignoreTests(unittest.TestCase):
    """Regression for the live-bench crash: a target repo without a .gitignore
    must not commit director's own .director provider files via `git add -A`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-gi-"))
        init_repo(self.tmp)

    def test_seeded_gitignore_excludes_runtime_files(self):
        from director import gitutil, setup

        setup.ensure_director_gitignore(self.tmp)
        fdir = self.tmp / ".director"
        (fdir / "config.toml").write_text("x")
        (fdir / "costs.jsonl").write_text("{}\n")
        (fdir / "plan.json").write_text("{}")
        (fdir / "logs").mkdir()
        (fdir / "logs" / "a.jsonl").write_text("{}")
        # emulate a director commit (test-author / node merge use git add -A)
        gitutil.commit_all("director: tests", self.tmp)
        tracked = gitutil.git(["ls-files"], self.tmp).stdout.splitlines()
        self.assertIn(".director/.gitignore", tracked)
        self.assertIn(".director/config.toml", tracked)  # config stays tracked
        self.assertNotIn(".director/costs.jsonl", tracked)  # provider ignored
        self.assertNotIn(".director/plan.json", tracked)
        self.assertNotIn(".director/logs/a.jsonl", tracked)


class PruneStaleDirectorTestsTests(unittest.TestCase):
    """`director plan` re-run must prune director-authored test files that the
    PRIOR plan referenced but the CURRENT plan no longer does — while never
    touching hand-written suite files or files the current plan still claims.

    Stubs director.plan.run_agent (the single external boundary), exactly as
    RunMetricsTests stubs run.run_agent, and exercises the real prune logic
    against a real temp git repo.
    """

    HANDWRITTEN = "tests/test_handwritten.py"
    STALE = "tests/test_stale.py"
    SHARED = "tests/test_shared.py"
    CURRENT = "tests/test_current.py"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-prune-"))
        init_repo(self.tmp)
        self.fdir = self.tmp / ".director"
        self.fdir.mkdir()
        (self.fdir / "logs").mkdir()
        (self.tmp / "tests").mkdir()
        # A hand-written suite file (never in any DAG) plus the three plan-owned
        # files: a prior-only "stale" file, a shared file in both plans, and the
        # current file. All committed so a later deletion shows up in git.
        for rel in (self.HANDWRITTEN, self.STALE, self.SHARED, self.CURRENT):
            (self.tmp / rel).write_text("# placeholder test file\n")
        (self.tmp / "tests" / "__init__.py").write_text("")
        git(["add", "-A"], self.tmp)
        git(["commit", "-qm", "seed test files"], self.tmp)
        self.cfg = mk_config(self.tmp)
        self.ledger_path = self.fdir / "costs.jsonl"

    # -- helpers ----------------------------------------------------------- #
    def _ledger(self):
        from director.cost import CostLedger

        return CostLedger(self.ledger_path)

    def _ok_result(self, log_path):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("{}\n")
        return RunResult(
            returncode=0,
            text="done",
            tokens={"input": 10, "output": 5, "reasoning": 0, "total": 15},
            cost_reported=0.0,
            n_steps=1,
            tool_calls=[],
            tool_events=[],
            error=None,
            timed_out=False,
            log_path=str(log_path),
        )

    def _current_plan(self):
        """In-memory current plan: one node owning the CURRENT + SHARED tests."""
        from director.models import Plan

        node = Node(
            id="current",
            title="current",
            spec="implement the thing",
            files=["mod_current.py"],
            depends_on=[],
            test_cmd="false",  # nonzero == red, so no spurious warning
            tests=[self.CURRENT, self.SHARED],
            test_hashes={},
        )
        return Plan(
            job_id="T",
            task="t",
            repo=str(self.tmp),
            created_at="now",
            job_branch="director/job-T",
            nodes=[node],
        )

    def _prior_tests(self):
        return {self.STALE, self.SHARED}

    # -- direct _author_tests path ----------------------------------------- #
    def test_author_tests_prunes_stale_keeps_handwritten_and_shared(self):
        from director import plan as planmod

        orig = planmod.run_agent
        planmod.run_agent = lambda *, agent, model, message, cwd, log_path, timeout: (
            self._ok_result(log_path)
        )
        try:
            planmod._author_tests(
                self._current_plan(),
                self.tmp,
                self.cfg,
                self._ledger(),
                self.fdir / "logs",
                lambda m: None,
                prev_tests=self._prior_tests(),
            )
        finally:
            planmod.run_agent = orig

        # (a) the stale prior-attempt file is deleted from disk
        self.assertFalse((self.tmp / self.STALE).exists())
        # (b) the hand-written suite file is untouched
        self.assertTrue((self.tmp / self.HANDWRITTEN).exists())
        # (c) a file referenced by BOTH prior and current plans survives
        self.assertTrue((self.tmp / self.SHARED).exists())
        # the current file the new plan owns is still present
        self.assertTrue((self.tmp / self.CURRENT).exists())

    def test_author_tests_none_prev_prunes_nothing(self):
        """The prev_tests=None default makes pruning a no-op."""
        from director import plan as planmod

        orig = planmod.run_agent
        planmod.run_agent = lambda *, agent, model, message, cwd, log_path, timeout: (
            self._ok_result(log_path)
        )
        try:
            planmod._author_tests(
                self._current_plan(),
                self.tmp,
                self.cfg,
                self._ledger(),
                self.fdir / "logs",
                lambda m: None,
            )
        finally:
            planmod.run_agent = orig

        for rel in (self.HANDWRITTEN, self.STALE, self.SHARED, self.CURRENT):
            self.assertTrue((self.tmp / rel).exists(), f"{rel} must survive")

    def test_author_tests_missing_stale_file_is_silent_noop(self):
        """A stale path already gone from disk must not raise."""
        from director import plan as planmod

        (self.tmp / self.STALE).unlink()  # gone before the prune runs
        orig = planmod.run_agent
        planmod.run_agent = lambda *, agent, model, message, cwd, log_path, timeout: (
            self._ok_result(log_path)
        )
        try:
            planmod._author_tests(
                self._current_plan(),
                self.tmp,
                self.cfg,
                self._ledger(),
                self.fdir / "logs",
                lambda m: None,
                prev_tests=self._prior_tests(),
            )
        finally:
            planmod.run_agent = orig
        self.assertFalse((self.tmp / self.STALE).exists())
        self.assertTrue((self.tmp / self.SHARED).exists())

    # -- decompose path (reads prior plan.json from disk) ------------------ #
    def _planner_json(self):
        return json.dumps(
            {
                "nodes": [
                    {
                        "id": "current",
                        "title": "current",
                        "spec": "implement the thing",
                        "files": ["mod_current.py"],
                        "depends_on": [],
                        "test_cmd": "false",
                        "tests": [self.CURRENT, self.SHARED],
                        "estimated_difficulty": "easy",
                    }
                ]
            }
        )

    def _fake_agent(self):
        def fake(*, agent, model, message, cwd, log_path, timeout):
            res = self._ok_result(log_path)
            if agent == "planner":
                res.text = self._planner_json()
            return res

        return fake

    def _write_prior_plan(self):
        prior = {
            "job_id": "T",
            "task": "t",
            "repo": str(self.tmp),
            "created_at": "now",
            "job_branch": "director/job-T",
            "nodes": [
                {
                    "id": "old",
                    "title": "old",
                    "spec": "old work",
                    "files": ["mod_old.py"],
                    "depends_on": [],
                    "test_cmd": "false",
                    "tests": [self.STALE, self.SHARED],
                    "estimated_difficulty": "easy",
                    "test_hashes": {},
                }
            ],
        }
        (self.fdir / "plan.json").write_text(json.dumps(prior))

    def _prog(self):
        from director.plan import PlanProgress

        return PlanProgress(
            job_id="T",
            task="t",
            job_branch="director/job-T",
            stage="decompose",
            auto=True,
            critique=False,
        )

    def test_decompose_prunes_stale_and_commits_deletion(self):
        from director import plan as planmod

        self._write_prior_plan()
        (self.fdir / "spec.md").write_text("# Spec\n")
        orig = planmod.run_agent
        planmod.run_agent = self._fake_agent()
        try:
            planmod._stage_bc_decompose(
                self._prog(), self.tmp, self.cfg, self._ledger(), self.fdir / "logs", lambda m: None
            )
        finally:
            planmod.run_agent = orig

        # stale file pruned from disk and from git tracking
        self.assertFalse((self.tmp / self.STALE).exists())
        tracked = gitutil.git(["ls-files"], self.tmp).stdout.splitlines()
        self.assertNotIn(self.STALE, tracked)
        # (e) the deletion is reflected in the commit director just made
        head = gitutil.git(["show", "--name-status", "HEAD"], self.tmp).stdout
        self.assertIn(self.STALE, head)
        self.assertIn("D", head.split(self.STALE)[0].splitlines()[-1])
        # hand-written + shared + current all survive
        self.assertTrue((self.tmp / self.HANDWRITTEN).exists())
        self.assertTrue((self.tmp / self.SHARED).exists())
        self.assertTrue((self.tmp / self.CURRENT).exists())

    def test_decompose_no_prior_plan_prunes_nothing(self):
        from director import plan as planmod

        # (d) no prior plan.json on disk → nothing is deleted
        self.assertFalse((self.fdir / "plan.json").exists())
        (self.fdir / "spec.md").write_text("# Spec\n")
        orig = planmod.run_agent
        planmod.run_agent = self._fake_agent()
        try:
            planmod._stage_bc_decompose(
                self._prog(), self.tmp, self.cfg, self._ledger(), self.fdir / "logs", lambda m: None
            )
        finally:
            planmod.run_agent = orig

        for rel in (self.HANDWRITTEN, self.STALE, self.SHARED, self.CURRENT):
            self.assertTrue((self.tmp / rel).exists(), f"{rel} must survive")

    def test_decompose_corrupt_prior_plan_does_not_raise(self):
        from director import plan as planmod

        # an unparseable prior plan.json must yield prev_tests=set(), not an error
        (self.fdir / "plan.json").write_text("{ not valid json")
        (self.fdir / "spec.md").write_text("# Spec\n")
        orig = planmod.run_agent
        planmod.run_agent = self._fake_agent()
        try:
            planmod._stage_bc_decompose(
                self._prog(), self.tmp, self.cfg, self._ledger(), self.fdir / "logs", lambda m: None
            )
        finally:
            planmod.run_agent = orig
        # nothing pruned (no usable prior tests) and the stale file remains
        self.assertTrue((self.tmp / self.STALE).exists())


class CommitAllAndMergeBranchNoVerifyTests(unittest.TestCase):
    """Director's own commits/merges must bypass repo-configured hooks
    (commit-msg, pre-commit, etc.) — those exist for human contributors, and
    must never block the automated director loop."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-noverify-"))
        init_repo(self.tmp)

    def _capture_git_calls(self):
        """Spy on gitutil.git, recording every args list while still
        delegating to the real implementation."""
        calls = []
        orig = gitutil.git

        def spy(args, cwd, check=True):
            calls.append(list(args))
            return orig(args, cwd, check=check)

        gitutil.git = spy
        return calls, orig

    def test_commit_all_passes_no_verify_and_gpgsign_false(self):
        calls, orig = self._capture_git_calls()
        (self.tmp / "f.txt").write_text("hello\n")
        try:
            ok = gitutil.commit_all("director: change", self.tmp)
        finally:
            gitutil.git = orig
        self.assertTrue(ok)
        commit_calls = [c for c in calls if "commit" in c]
        self.assertEqual(len(commit_calls), 1)
        commit_args = commit_calls[0]
        self.assertIn("--no-verify", commit_args)
        self.assertIn("commit.gpgsign=false", commit_args)

    def test_commit_all_returns_false_when_nothing_to_commit(self):
        ok = gitutil.commit_all("director: noop", self.tmp)
        self.assertFalse(ok)

    def test_merge_branch_passes_no_verify_with_message(self):
        gitutil.git(["checkout", "-q", "-b", "feature"], self.tmp)
        (self.tmp / "g.txt").write_text("feature change\n")
        gitutil.git(["add", "-A"], self.tmp)
        gitutil.git(
            ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "feature commit"], self.tmp
        )
        gitutil.git(["checkout", "-q", "master"], self.tmp)

        calls, orig = self._capture_git_calls()
        try:
            result = gitutil.merge_branch("feature", self.tmp, message="merge feature")
        finally:
            gitutil.git = orig
        self.assertEqual(result.returncode, 0)
        merge_calls = [c for c in calls if "merge" in c]
        self.assertEqual(len(merge_calls), 1)
        merge_args = merge_calls[0]
        self.assertIn("--no-ff", merge_args)
        self.assertIn("--no-verify", merge_args)
        self.assertIn("commit.gpgsign=false", merge_args)

    def test_merge_branch_passes_no_verify_without_message(self):
        gitutil.git(["checkout", "-q", "-b", "feature2"], self.tmp)
        (self.tmp / "h.txt").write_text("feature2 change\n")
        gitutil.git(["add", "-A"], self.tmp)
        gitutil.git(
            ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "feature2 commit"], self.tmp
        )
        gitutil.git(["checkout", "-q", "master"], self.tmp)

        calls, orig = self._capture_git_calls()
        try:
            result = gitutil.merge_branch("feature2", self.tmp)
        finally:
            gitutil.git = orig
        self.assertEqual(result.returncode, 0)
        merge_calls = [c for c in calls if "merge" in c]
        self.assertEqual(len(merge_calls), 1)
        merge_args = merge_calls[0]
        self.assertIn("--no-ff", merge_args)
        self.assertIn("--no-verify", merge_args)

    def test_commit_all_bypasses_failing_commit_msg_hook(self):
        hooks_dir = self.tmp / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        gitutil.git(["config", "core.hooksPath", str(hooks_dir)], self.tmp)
        hook_path = hooks_dir / "commit-msg"
        hook_path.write_text("#!/bin/sh\nexit 1\n")
        hook_path.chmod(0o755)

        before = int(gitutil.git(["rev-list", "--count", "HEAD"], self.tmp).stdout.strip())
        (self.tmp / "blocked.txt").write_text("should still commit\n")
        ok = gitutil.commit_all("director: bypass hook", self.tmp)
        self.assertTrue(ok)
        after = int(gitutil.git(["rev-list", "--count", "HEAD"], self.tmp).stdout.strip())
        self.assertEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
