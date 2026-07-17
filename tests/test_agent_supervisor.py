"""Supervisor lifecycle (director/agent/supervisor.py) against fake runners.

Real subprocesses stand in for runners (via DIRECTOR_AGENT_RUNNER) so the
tests exercise the actual spawn/heartbeat/reap/killpg machinery, but no model
provider is ever invoked and only temporary repositories are touched.
"""

import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import gitutil  # noqa: E402
from director.agent import storage  # noqa: E402
from director.agent.storage import JobStore, RequestConflictError, UnknownJobError  # noqa: E402
from director.agent.supervisor import (  # noqa: E402
    MAX_RESUMES,
    StateConflictError,
    SubmitError,
    Supervisor,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# A well-behaved fake runner: prepares the workspace worktree exactly like the
# real one, heartbeats, commits work on the job branch, writes a result.
OK_RUNNER = """
import os, sys, time
sys.path.insert(0, {root!r})
from pathlib import Path
from director import gitutil
from director.agent.storage import JobStore, PREPARING, RUNNING, SUCCEEDED, utc_now

job_id = sys.argv[1]
store = JobStore()
job = store.load_job(job_id)
pid = os.getpid()
store.beat(job_id, pid)
store.set_state(job_id, PREPARING, pid=pid)
ws = store.workspace_dir(job_id)
gitutil.git(["worktree", "add", "-b", job["job_branch"], str(ws), job["base_commit"]],
            Path(job["repo"]))
store.set_state(job_id, RUNNING, pid=pid)
(ws / "work.txt").write_text(job_id)
gitutil.commit_all("fake work", ws)
deadline = time.time() + float(os.environ.get("FAKE_RUNNER_SLEEP", "0"))
while time.time() < deadline:
    store.beat(job_id, pid)
    time.sleep(0.1)
store.write_result(job_id, {{"ok": True, "error": None,
    "run": {{"integration_ok": True, "done": ["n1"], "failed": []}},
    "exit_code": 0, "finished_at": utc_now()}})
store.set_state(job_id, SUCCEEDED)
"""

# A runner that reaches `running`, spawns a grandchild in its process group,
# then sleeps forever — cancellation must kill the whole group.
SLEEPER_RUNNER = """
import os, subprocess, sys, time
sys.path.insert(0, {root!r})
from director.agent.storage import JobStore, RUNNING

job_id = sys.argv[1]
store = JobStore()
pid = os.getpid()
store.beat(job_id, pid)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
(store.job_dir(job_id) / "grandchild.pid").write_text(str(child.pid))
store.set_state(job_id, RUNNING, pid=pid)
while True:
    store.beat(job_id, pid)
    time.sleep(0.1)
"""

# A runner that dies without ever writing a result.
CRASH_RUNNER = """
import os, sys
sys.path.insert(0, {root!r})
from director.agent.storage import JobStore, RUNNING

job_id = sys.argv[1]
store = JobStore()
pid = os.getpid()
store.beat(job_id, pid)
store.set_state(job_id, RUNNING, pid=pid)
sys.exit(3)
"""


def _wait_until(cond, timeout=15.0, tick=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tick:
            tick()
        if cond():
            return True
        time.sleep(0.05)
    return False


class SupervisorTestCase(unittest.TestCase):
    """Shared fixture: temp agent home + temp git repo + env plumbing."""

    runner_script = OK_RUNNER

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        gitutil.git(["config", "user.email", "t@t"], self.repo)
        gitutil.git(["config", "user.name", "t"], self.repo)
        (self.repo / "README.md").write_text("hello\n")
        gitutil.commit_all("init", self.repo)
        self.head = gitutil.current_commit(self.repo)

        script = self.home / "fake_runner.py"
        script.write_text(self.runner_script.format(root=str(REPO_ROOT)))
        self._env = {
            "DIRECTOR_AGENT_HOME": str(self.home),
            "DIRECTOR_AGENT_RUNNER": f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        }
        self._saved = {k: os.environ.get(k) for k in (*self._env, "FAKE_RUNNER_SLEEP")}
        os.environ.update(self._env)
        self.store = JobStore(self.home)
        self.sup = Supervisor(self.store, max_jobs=2, log=lambda m: None)

    def tearDown(self):
        if os.name != "nt":  # runner-spawning tests are POSIX-only (skipped on Windows)
            own_pgid = os.getpgid(0)
            for job_id in self.store.job_ids():
                job = self.store.load_job(job_id)
                pid = job.get("pid")
                if not pid or not storage.pid_exists(int(pid)):
                    continue
                try:
                    if os.getpgid(int(pid)) == own_pgid:
                        continue  # some tests use the test process itself as a fake runner
                except OSError:
                    continue
                Supervisor._kill_group(int(pid))
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _submit(self, task="do it", **over):
        req = {"repo": str(self.repo), "task": task, **over}
        job, created = self.sup.submit(req)
        return job

    def _run_to_terminal(self, *job_ids, timeout=20.0):
        ok = _wait_until(
            lambda: all(
                self.store.load_job(j)["state"] in storage.TERMINAL_STATES for j in job_ids
            ),
            timeout=timeout,
            tick=self.sup.tick,
        )
        self.assertTrue(
            ok,
            f"jobs never reached a terminal state: "
            f"{[(j, self.store.load_job(j)['state']) for j in job_ids]}",
        )


class SubmitTests(SupervisorTestCase):
    def test_empty_task_rejected(self):
        with self.assertRaises(SubmitError) as ctx:
            self.sup.submit({"repo": str(self.repo), "task": "   "})
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_missing_repo_rejected(self):
        with self.assertRaises(SubmitError) as ctx:
            self.sup.submit({"repo": "/nope/definitely/missing", "task": "x"})
        self.assertEqual(ctx.exception.code, "invalid_repo")

    def test_non_git_dir_rejected(self):
        plain = Path(tempfile.mkdtemp())
        with self.assertRaises(SubmitError) as ctx:
            self.sup.submit({"repo": str(plain), "task": "x"})
        self.assertEqual(ctx.exception.code, "invalid_repo")

    def test_unresolvable_base_ref_rejected(self):
        with self.assertRaises(SubmitError) as ctx:
            self.sup.submit({"repo": str(self.repo), "task": "x", "base_ref": "no-such-branch"})
        self.assertEqual(ctx.exception.code, "base_ref_unresolvable")

    def test_submission_captures_immutable_base_and_identity(self):
        job = self._submit()
        self.assertEqual(job["state"], storage.QUEUED)
        self.assertEqual(job["base_commit"], self.head)
        self.assertEqual(job["base_ref"], "HEAD")
        self.assertEqual(Path(job["repo"]), self.repo.resolve())
        self.assertTrue(Path(job["git_common_dir"]).is_absolute())
        self.assertEqual(job["job_branch"], f"director/{job['job_id']}")

    def test_request_id_idempotency_and_conflict(self):
        job = self._submit(request_id="rid-1")
        again, created = self.sup.submit(
            {"repo": str(self.repo), "task": "do it", "request_id": "rid-1"}
        )
        self.assertFalse(created)
        self.assertEqual(again["job_id"], job["job_id"])
        with self.assertRaises(RequestConflictError):
            self.sup.submit({"repo": str(self.repo), "task": "DIFFERENT", "request_id": "rid-1"})


@unittest.skipIf(os.name == "nt", "spawns POSIX-session fake runners")
class ExecutionTests(SupervisorTestCase):
    def test_two_jobs_same_repo_run_concurrently_and_leave_checkout_untouched(self):
        os.environ["FAKE_RUNNER_SLEEP"] = "2"
        (self.repo / "dirty.txt").write_text("uncommitted\n")  # must survive untouched
        j1 = self._submit("first")["job_id"]
        j2 = self._submit("second")["job_id"]

        both_running = _wait_until(
            lambda: (
                {self.store.load_job(j1)["state"], self.store.load_job(j2)["state"]}
                == {storage.RUNNING}
            ),
            timeout=15,
            tick=self.sup.tick,
        )
        self.assertTrue(both_running, "expected both jobs RUNNING simultaneously")
        self._run_to_terminal(j1, j2)

        for jid in (j1, j2):
            job = self.store.load_job(jid)
            self.assertEqual(job["state"], storage.SUCCEEDED)
            self.assertTrue(gitutil.branch_exists(job["job_branch"], self.repo))
            shown = gitutil.git(["show", f"{job['job_branch']}:work.txt"], self.repo).stdout
            self.assertEqual(shown, jid)
        # the submitted checkout: same branch, same HEAD, uncommitted file intact
        self.assertEqual(gitutil.current_branch(self.repo), "main")
        self.assertEqual(gitutil.current_commit(self.repo), self.head)
        self.assertEqual((self.repo / "dirty.txt").read_text(), "uncommitted\n")

    def test_capacity_limits_concurrency(self):
        os.environ["FAKE_RUNNER_SLEEP"] = "3"  # wide margin: job 1 must still be
        self.sup.max_jobs = 1  # running when we probe that job 2 stayed queued
        j1 = self._submit("first")["job_id"]
        j2 = self._submit("second")["job_id"]
        self.sup.tick()

        def states():
            return [self.store.load_job(j)["state"] for j in (j1, j2)]

        # exactly one job got the single slot; the other stays queued
        self.assertTrue(
            _wait_until(lambda: sum(s != storage.QUEUED for s in states()) == 1, timeout=10)
        )
        self.sup.tick()  # capacity still full — must not spawn the second job
        self.assertEqual(sum(s == storage.QUEUED for s in states()), 1)
        self._run_to_terminal(j1, j2, timeout=30)
        self.assertEqual(states(), [storage.SUCCEEDED, storage.SUCCEEDED])

    def test_events_are_ordered_and_cursorable(self):
        j1 = self._submit("first")["job_id"]
        self._run_to_terminal(j1)
        events = self.store.read_events(j1)
        names = [e["event"] for e in events]
        self.assertEqual(names[0], "submitted")
        self.assertIn("runner_started", names)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(set(seqs)))
        cursor = seqs[2]
        self.assertEqual([e["seq"] for e in self.store.read_events(j1, after=cursor)], seqs[3:])


@unittest.skipIf(os.name == "nt", "asserts POSIX process-group kill semantics")
class CancelTests(SupervisorTestCase):
    runner_script = SLEEPER_RUNNER

    def test_cancel_kills_whole_process_group_and_is_idempotent(self):
        jid = self._submit("hang forever")["job_id"]
        self.assertTrue(
            _wait_until(
                lambda: self.store.load_job(jid)["state"] == storage.RUNNING,
                tick=self.sup.tick,
            )
        )
        runner_pid = self.store.load_job(jid)["pid"]
        grand_pid = int((self.store.job_dir(jid) / "grandchild.pid").read_text())
        self.assertTrue(storage.pid_exists(runner_pid))
        self.assertTrue(storage.pid_exists(grand_pid))

        job = self.sup.cancel(jid)
        self.assertEqual(job["state"], storage.CANCELLED)
        self.assertTrue(_wait_until(lambda: not storage.pid_exists(runner_pid), timeout=10))
        self.assertTrue(_wait_until(lambda: not storage.pid_exists(grand_pid), timeout=10))
        result = self.store.read_result(jid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "cancelled")
        # idempotent: cancelling a terminal job reports the state, changes nothing
        again = self.sup.cancel(jid)
        self.assertEqual(again["state"], storage.CANCELLED)

    def test_cancel_queued_job_never_starts_a_runner(self):
        jid = self._submit("never run")["job_id"]
        job = self.sup.cancel(jid)
        self.assertEqual(job["state"], storage.CANCELLED)
        self.sup.tick()
        self.assertEqual(self.store.load_job(jid)["state"], storage.CANCELLED)

    def test_cancel_unknown_job_raises(self):
        with self.assertRaises(UnknownJobError):
            self.sup.cancel("job-00000000-000000-ffffff")


@unittest.skipIf(os.name == "nt", "spawns POSIX-session fake runners")
class CrashRecoveryTests(SupervisorTestCase):
    runner_script = CRASH_RUNNER

    def test_crashing_runner_is_resumed_then_failed(self):
        jid = self._submit("always crash")["job_id"]
        self.assertTrue(
            _wait_until(
                lambda: self.store.load_job(jid)["state"] == storage.FAILED,
                timeout=30,
                tick=self.sup.tick,
            )
        )
        job = self.store.load_job(jid)
        self.assertEqual(job["reason"], "too_many_interruptions")
        self.assertEqual(job["resume_count"], MAX_RESUMES)
        states = [e["data"]["state"] for e in self.store.read_events(jid) if e["event"] == "state"]
        self.assertIn(storage.INTERRUPTED, states)
        # a durable result exists so `wait` terminates
        self.assertFalse(self.store.read_result(jid)["ok"])


class ReconcileTests(SupervisorTestCase):
    def _make_job(self) -> str:
        return self._submit("recon target")["job_id"]

    def test_dead_runner_without_result_becomes_interrupted_and_requeues(self):
        jid = self._make_job()
        dead_pid = 2**22 + 4321
        self.store.set_state(jid, storage.RUNNING, pid=dead_pid)
        fresh = Supervisor(self.store, log=lambda m: None)
        fresh.reconcile()
        job = self.store.load_job(jid)
        self.assertEqual(job["state"], storage.QUEUED)  # interrupted → re-queued to resume
        self.assertEqual(job["resume_count"], 1)
        states = [e["data"]["state"] for e in self.store.read_events(jid) if e["event"] == "state"]
        self.assertIn(storage.INTERRUPTED, states)

    def test_live_runner_is_adopted_not_disturbed(self):
        jid = self._make_job()
        self.store.beat(jid, os.getpid())  # our own pid: alive + fresh heartbeat
        self.store.set_state(jid, storage.RUNNING, pid=os.getpid())
        fresh = Supervisor(self.store, log=lambda m: None)
        fresh.reconcile()
        self.assertEqual(self.store.load_job(jid)["state"], storage.RUNNING)
        self.assertIn(jid, fresh._adopted)

    def test_existing_result_is_reflected_into_registry(self):
        jid = self._make_job()
        self.store.set_state(jid, storage.RUNNING, pid=2**22 + 77)
        self.store.write_result(jid, {"ok": True, "error": None, "run": {}, "exit_code": 0})
        fresh = Supervisor(self.store, log=lambda m: None)
        fresh.reconcile()
        self.assertEqual(self.store.load_job(jid)["state"], storage.SUCCEEDED)

    def test_corrupt_job_json_with_readable_request_becomes_lost(self):
        jid = self._make_job()
        self.store.job_path(jid).write_text("{ definitely not json")
        fresh = Supervisor(self.store, log=lambda m: None)
        fresh.reconcile()
        job = self.store.load_job(jid)
        self.assertEqual(job["state"], storage.LOST)
        self.assertEqual(job["reason"], "corrupt_state")


class RetryPruneTests(SupervisorTestCase):
    def _failed_job(self) -> str:
        jid = self._submit("to fail")["job_id"]
        art = self.store.artifacts_dir(jid)
        (art / "state.json").write_text(
            '{"job_id": "x", "nodes": {"n1": {"id": "n1", "status": "failed", "error": "boom"}}}'
        )
        self.store.write_result(
            jid, {"ok": False, "error": {"code": "gates_failed", "message": "x"}, "exit_code": 1}
        )
        self.store.set_state(jid, storage.FAILED, reason="gates_failed")
        return jid

    def test_retry_requeues_and_resets_failed_nodes(self):
        jid = self._failed_job()
        job = self.sup.retry(jid)
        self.assertEqual(job["state"], storage.QUEUED)
        self.assertIsNone(self.store.read_result(jid))
        state = storage.read_json(self.store.artifacts_dir(jid) / "state.json")
        self.assertEqual(state["nodes"]["n1"]["status"], "pending")
        self.assertIsNone(state["nodes"]["n1"]["error"])

    def test_retry_conflicts_for_active_or_succeeded_jobs(self):
        jid = self._submit("active")["job_id"]
        with self.assertRaises(StateConflictError):
            self.sup.retry(jid)  # queued
        self.store.write_result(jid, {"ok": True, "error": None, "run": {}, "exit_code": 0})
        self.store.set_state(jid, storage.SUCCEEDED)
        with self.assertRaises(StateConflictError):
            self.sup.retry(jid)

    def test_prune_removes_terminal_jobs_but_keeps_active_and_branches(self):
        done = self._failed_job()
        active = self._submit("still queued")["job_id"]
        pruned = self.sup.prune()
        self.assertEqual(pruned, [done])
        self.assertFalse(self.store.job_dir(done).exists())
        self.assertTrue(self.store.job_dir(active).exists())

    def test_prune_unknown_explicit_id_raises(self):
        with self.assertRaises(UnknownJobError):
            self.sup.prune(["job-00000000-000000-ffffff"])


if __name__ == "__main__":
    unittest.main()
