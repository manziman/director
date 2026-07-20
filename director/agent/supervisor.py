"""The job supervisor: accept, schedule, monitor, recover, cancel.

One Supervisor instance lives inside `director agent serve`. It owns the queue
and the runner child processes, but deliberately NOT lifecycle truth — each
runner heartbeats and writes its own terminal result, so the supervisor can
crash/restart and reconcile from disk:

  - a live runner (fresh heartbeat AND live pid) is re-adopted;
  - an existing result.json is reflected into the registry;
  - a dead runner without a result becomes `interrupted` and is re-queued
    (the runner resumes from the job's persisted artifacts) up to
    MAX_RESUMES times before the job is failed;
  - a stale PID alone is never treated as a live runner.

Runners are spawned in their own process groups so cancellation can terminate
the whole job tree (provider subprocesses included) with one killpg.
"""

from __future__ import annotations

import calendar
import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from director import gitutil
from director.agent import storage
from director.agent.storage import (
    ACTIVE_STATES,
    CANCELLED,
    FAILED,
    INTERRUPTED,
    LOST,
    PREPARING,
    QUEUED,
    RUNNING,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    JobStore,
    RequestConflictError,
    UnknownJobError,
)

MAX_RESUMES = 2
CANCEL_GRACE_SECS = 5.0


class SubmitError(ValueError):
    """An invalid submission (bad repo, unresolvable ref, empty task)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StateConflictError(ValueError):
    """The requested mutation does not apply to the job's current state."""


def _fingerprint(request: dict) -> dict:
    """The fields that make two submissions 'the same request' for --request-id."""
    return {
        "repo": request.get("repo"),
        "task": request.get("task"),
        "base_ref": request.get("base_ref"),
        "settings": request.get("settings"),
        "labels": request.get("labels"),
    }


def default_runner_cmd() -> list[str]:
    env = os.environ.get("DIRECTOR_AGENT_RUNNER")
    if env:
        return shlex.split(env)
    return [sys.executable, "-m", "director", "agent", "run-job"]


def _secs_since(ts: str | None) -> float:
    """Seconds since an RFC 3339 UTC timestamp; unparseable → +inf (treat as old)."""
    if not ts:
        return float("inf")
    try:
        return max(0.0, time.time() - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return float("inf")


class Supervisor:
    def __init__(self, store: JobStore, max_jobs: int = 2, log=None):
        self.store = store
        self.max_jobs = max(1, int(max_jobs))
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self._children: dict[str, subprocess.Popen] = {}
        self._adopted: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self.reconcile()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="supervisor")
        self._thread.start()

    def stop(self) -> None:
        """Stop scheduling. Running jobs keep running — their runners are
        independent process groups and will be re-adopted on the next start."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            with contextlib.suppress(Exception):
                self.tick()

    # ---- submission ---------------------------------------------------------
    def submit(self, req: dict) -> tuple[dict, bool]:
        """Durably accept a job. Returns (job_doc, created).

        Resolves and captures repository identity and the base commit at
        submission time so the job is immune to the checkout moving on.
        """
        repo_arg = req.get("repo") or ""
        task = (req.get("task") or "").strip()
        if not task:
            raise SubmitError("invalid_request", "a non-empty task is required")
        repo = Path(repo_arg).expanduser()
        if not repo.is_dir():
            raise SubmitError("invalid_repo", f"repository path not found: {repo_arg}")
        repo = repo.resolve()
        common = gitutil.git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], repo, check=False
        )
        if common.returncode != 0:
            raise SubmitError("invalid_repo", f"not a git repository: {repo}")
        base_ref = req.get("base_ref") or "HEAD"
        rev = gitutil.git(["rev-parse", "--verify", base_ref + "^{commit}"], repo, check=False)
        if rev.returncode != 0:
            raise SubmitError(
                "base_ref_unresolvable", f"cannot resolve base ref {base_ref!r} in {repo}"
            )
        base_commit = rev.stdout.strip()

        # Resolve and validate the configuration NOW: an accepted job must be
        # immune to later edits of the checkout's .director/config.toml (the
        # runner rebuilds its Config from this snapshot, never from live files),
        # and a broken config should fail the submission, not the runner.
        from director import config as config_mod

        try:
            cfg_snapshot = config_mod.snapshot(repo)
            config_mod.from_snapshot(cfg_snapshot, repo / ".director" / "config.toml")
        except (FileNotFoundError, ValueError) as e:
            raise SubmitError("config_invalid", str(e)) from e

        settings = {
            "parallel": int(req.get("parallel") or 1),
            "max_attempts": int(req.get("max_attempts") or 0),
            "max_cost": float(req.get("max_cost") or 0.0),
            "no_critique": bool(req.get("no_critique") or False),
        }
        request = {
            "schema_version": SCHEMA_VERSION,
            "request_id": req.get("request_id"),
            "repo": str(repo),
            "git_common_dir": common.stdout.strip(),
            "task": task,
            "base_ref": base_ref,
            "base_commit": base_commit,
            "settings": settings,
            "labels": dict(req.get("labels") or {}),
            "submitted_at": storage.utc_now(),
        }

        with self._lock:
            rid = request["request_id"]
            if rid:
                prior = self.store.find_by_request_id(rid)
                if prior is not None:
                    if _fingerprint(prior) != _fingerprint(request):
                        raise RequestConflictError(
                            f"request-id {rid!r} was already used with different content "
                            f"(job {prior['job_id']})"
                        )
                    return self.store.load_job(prior["job_id"]), False
            job_id = storage.new_job_id()
            while self.store.job_dir(job_id).exists():  # same-second resubmission
                job_id = storage.new_job_id()
            request["job_id"] = job_id
            request["job_branch"] = storage.job_branch_for(job_id)
            job = self.store.create_job(request)
            storage.atomic_write_json(self.store.config_path(job_id), cfg_snapshot)
        self.log(f"[agent] accepted {job_id} for {repo} @ {base_commit[:12]}")
        return job, True

    # ---- scheduling / monitoring --------------------------------------------
    def tick(self) -> None:
        with self._lock:
            self._reap_children()
            self._check_adopted()
            self._fill_capacity()

    def _running_count(self) -> int:
        return len(self._children) + len(self._adopted)

    def _fill_capacity(self) -> None:
        if self._running_count() >= self.max_jobs:
            return
        queued = [
            j
            for j in (self.store.load_job(jid) for jid in self.store.job_ids())
            # a job we already track (spawned child or re-adopted runner) must
            # never get a second runner, whatever its registry state says
            if j["state"] == QUEUED
            and j["job_id"] not in self._children
            and j["job_id"] not in self._adopted
        ]
        queued.sort(key=lambda j: (j.get("submitted_at") or "", j["job_id"]))
        for job in queued:
            if self._running_count() >= self.max_jobs:
                break
            if not self._claim_queued(job):
                continue
            self._spawn(job)

    def _claim_queued(self, job: dict) -> bool:
        """True if `job` is safe to spawn a runner for. A queued job can carry a
        recorded PID when a previous supervisor died between _spawn() and the
        runner reaching `preparing` — starting a second runner then would race
        the live one. Adopt a live runner; give a freshly recorded PID one
        heartbeat window to prove itself; only then treat it as dead."""
        pid = job.get("pid")
        if not pid:
            return True
        job_id = job["job_id"]
        if self.store.runner_alive(job):
            self._adopted.add(job_id)
            self.log(f"[agent] re-adopted just-spawned runner for {job_id}")
            return False
        if storage.pid_exists(int(pid)) and _secs_since(job.get("updated_at")) < storage.STALE_SECS:
            return False  # may still be booting (no fresh heartbeat yet) — recheck next tick
        self.store.update_job(job_id, pid=None)  # dead (or recycled PID) — spawn cleanly
        return True

    def _spawn(self, job: dict) -> None:
        from director.proc import popen_tree

        job_id = job["job_id"]
        log_path = self.store.job_dir(job_id) / "runner.log"
        env = {**os.environ, "DIRECTOR_AGENT_HOME": str(self.store.root)}
        try:
            with open(log_path, "ab") as logf:
                proc_obj = popen_tree(
                    [*default_runner_cmd(), job_id],
                    cwd=str(self.store.root),
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                )
        except OSError as e:
            self.store.write_result(
                job_id,
                {
                    "ok": False,
                    "error": {"code": "spawn_failed", "message": str(e)},
                    "run": None,
                    "exit_code": None,
                    "finished_at": storage.utc_now(),
                },
            )
            self.store.set_state(
                job_id,
                FAILED,
                reason="spawn_failed",
                error={"code": "spawn_failed", "message": str(e)},
            )
            return
        self._children[job_id] = proc_obj
        self.store.update_job(job_id, pid=proc_obj.pid)
        self.store.append_event(job_id, "runner_started", {"pid": proc_obj.pid})
        self.log(f"[agent] runner for {job_id} started (pid {proc_obj.pid})")

    def _reap_children(self) -> None:
        for job_id, proc_obj in list(self._children.items()):
            rc = proc_obj.poll()
            if rc is None:
                continue
            del self._children[job_id]
            self._finalize_dead(job_id, exit_code=rc)

    def _check_adopted(self) -> None:
        for job_id in list(self._adopted):
            try:
                job = self.store.load_job(job_id)
            except UnknownJobError:
                self._adopted.discard(job_id)
                continue
            if job["state"] in TERMINAL_STATES:
                self._adopted.discard(job_id)
            elif not self.store.runner_alive(job):
                self._adopted.discard(job_id)
                self._finalize_dead(job_id, exit_code=None)

    def _finalize_dead(self, job_id: str, exit_code: int | None) -> None:
        """A runner process is gone. Reflect its result, or mark the job
        interrupted and re-queue it for resume."""
        job = self.store.load_job(job_id)
        if job["state"] in TERMINAL_STATES:
            return
        result = self.store.read_result(job_id)
        if result is not None:
            # Runner finished but died before (or while) updating job.json.
            state = storage.SUCCEEDED if result.get("ok") else FAILED
            self.store.set_state(
                job_id,
                state,
                reason=(result.get("error") or {}).get("code"),
                error=result.get("error"),
                exit_code=result.get("exit_code", exit_code),
            )
            return
        self.store.set_state(job_id, INTERRUPTED, reason="runner_died", exit_code=exit_code)
        self._resume_or_fail(job_id, exit_code)

    def _resume_or_fail(self, job_id: str, exit_code: int | None = None) -> None:
        """An interrupted job resumes from its persisted artifacts — but only
        MAX_RESUMES times, so a crash-looping runner ends as a stable failure
        with a durable result instead of respawning forever."""
        job = self.store.load_job(job_id)
        resumes = int(job.get("resume_count") or 0)
        if resumes < MAX_RESUMES:
            self.store.update_job(job_id, resume_count=resumes + 1, pid=None)
            self.store.set_state(job_id, QUEUED, reason="resume")
            self.log(f"[agent] {job_id} interrupted — re-queued (resume {resumes + 1})")
        else:
            error = {
                "code": "too_many_interruptions",
                "message": f"runner died {resumes + 1} times without a result",
            }
            self.store.write_result(
                job_id,
                {
                    "ok": False,
                    "error": error,
                    "run": None,
                    "exit_code": exit_code,
                    "finished_at": storage.utc_now(),
                },
            )
            self.store.set_state(job_id, FAILED, reason=error["code"], error=error)

    # ---- restart reconciliation --------------------------------------------
    def reconcile(self) -> None:
        with self._lock:
            for job_id in self._registry_ids_repairing_corrupt():
                job = self.store.load_job(job_id)
                state = job["state"]
                if state in TERMINAL_STATES:
                    continue
                result = self.store.read_result(job_id)
                if result is not None:
                    self._finalize_dead(job_id, exit_code=result.get("exit_code"))
                elif state in (PREPARING, RUNNING):
                    if self.store.runner_alive(job):
                        self._adopted.add(job_id)
                        self.log(f"[agent] re-adopted live runner for {job_id}")
                    else:
                        self._finalize_dead(job_id, exit_code=None)
                elif state == INTERRUPTED:
                    # Interrupted while the agent was down: resume it (bounded).
                    self._resume_or_fail(job_id)
                # QUEUED jobs are picked up by the next tick; _fill_capacity
                # handles the queued-with-recorded-PID window there.

    def _registry_ids_repairing_corrupt(self) -> list[str]:
        """Job dirs with an unreadable job.json but a readable request.json are
        rebuilt as LOST — a stable, listable state instead of a traceback."""
        ids = []
        if self.store.jobs_root.is_dir():
            for p in sorted(self.store.jobs_root.iterdir()):
                if not p.name.startswith("job-"):
                    continue
                if storage.read_json(p / "job.json") is not None:
                    ids.append(p.name)
                    continue
                req = storage.read_json(p / "request.json")
                if req is None:
                    continue  # nothing usable to rebuild from
                now = storage.utc_now()
                storage.atomic_write_json(
                    p / "job.json",
                    {
                        "schema_version": SCHEMA_VERSION,
                        "job_id": p.name,
                        "request_id": req.get("request_id"),
                        "labels": req.get("labels", {}),
                        "state": LOST,
                        "reason": "corrupt_state",
                        "error": {
                            "code": "corrupt_state",
                            "message": "job.json was unreadable; job cannot be resumed",
                        },
                        "repo": req.get("repo"),
                        "git_common_dir": req.get("git_common_dir"),
                        "base_ref": req.get("base_ref"),
                        "base_commit": req.get("base_commit"),
                        "job_branch": req.get("job_branch"),
                        "workspace": str(self.store.workspace_dir(p.name)),
                        "artifact_dir": str(self.store.artifacts_dir(p.name)),
                        "settings": req.get("settings", {}),
                        "submitted_at": req.get("submitted_at"),
                        "started_at": None,
                        "completed_at": now,
                        "updated_at": now,
                        "pid": None,
                        "resume_count": 0,
                        "exit_code": None,
                    },
                )
                ids.append(p.name)
        return ids

    # ---- mutations ----------------------------------------------------------
    def cancel(self, job_id: str) -> dict:
        """Idempotent cancel: kills the job's whole process group, then records
        a durable terminal state. Cancelling a terminal job is a no-op."""
        with self._lock:
            job = self.store.load_job(job_id)
            if job["state"] in TERMINAL_STATES:
                return job
            proc_obj = self._children.pop(job_id, None)
            self._adopted.discard(job_id)
            pid = proc_obj.pid if proc_obj is not None else job.get("pid")
            if pid and storage.pid_exists(int(pid)):
                self._kill_group(int(pid))
            if proc_obj is not None:
                with contextlib.suppress(Exception):
                    proc_obj.wait(timeout=5)
            error = {"code": "cancelled", "message": "cancelled by request"}
            self.store.write_result(
                job_id,
                {
                    "ok": False,
                    "error": error,
                    "run": None,
                    "exit_code": None,
                    "finished_at": storage.utc_now(),
                },
            )
            return self.store.set_state(job_id, CANCELLED, reason="cancelled", error=error)

    @staticmethod
    def _kill_group(pid: int) -> None:
        """Terminate the job's whole process tree: SIGTERM the group, give it
        CANCEL_GRACE_SECS, then SIGKILL. Windows has no process groups to
        signal — taskkill /T fells the tree in one step."""
        if os.name == "nt":  # pragma: no cover - exercised only on Windows
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False
            )
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.time() + CANCEL_GRACE_SECS
            while time.time() < deadline:
                if not storage.pid_exists(pid):
                    return
                time.sleep(0.1)
            os.killpg(pgid, signal.SIGKILL)

    def retry(self, job_id: str) -> dict:
        """Re-queue a failed/cancelled/interrupted/lost job. The runner resumes
        from the job's persisted artifacts; failed nodes are reset to pending."""
        with self._lock:
            job = self.store.load_job(job_id)
            if job["state"] not in (FAILED, CANCELLED, INTERRUPTED, LOST):
                raise StateConflictError(f"cannot retry a job in state {job['state']!r}")
            with contextlib.suppress(OSError):
                self.store.result_path(job_id).unlink()
            self._reset_failed_nodes(self.store.artifacts_dir(job_id) / "state.json")
            self.store.update_job(job_id, pid=None, exit_code=None, error=None)
            return self.store.set_state(job_id, QUEUED, reason="retry")

    @staticmethod
    def _reset_failed_nodes(state_path: Path) -> None:
        doc = storage.read_json(state_path)
        if not doc:
            return
        changed = False
        for ns in doc.get("nodes", {}).values():
            if ns.get("status") in ("failed", "escalated", "running"):
                ns["status"] = "pending"
                ns["error"] = None
                changed = True
        if changed:
            storage.atomic_write_json(state_path, doc)

    def prune(self, job_ids: list[str] | None = None) -> list[str]:
        """Delete terminal jobs' storage (workspace, worktrees, artifacts) and
        drop their worktree registrations from the source repository. Job
        branches are left in place — results stay visible from the repo."""
        with self._lock:
            targets = job_ids if job_ids is not None else self.store.job_ids()
            pruned: list[str] = []
            for job_id in targets:
                try:
                    job = self.store.load_job(job_id)
                except UnknownJobError:
                    if job_ids is not None:
                        raise
                    continue
                if job["state"] not in TERMINAL_STATES:
                    continue
                repo = Path(job.get("repo") or "")
                shutil.rmtree(self.store.job_dir(job_id), ignore_errors=True)
                if (repo / ".git").exists():
                    gitutil.git(["worktree", "prune"], repo, check=False)
                pruned.append(job_id)
            return pruned

    # ---- status -------------------------------------------------------------
    def counts(self) -> dict:
        with self._lock:
            by_state: dict[str, int] = {}
            for jid in self.store.job_ids():
                with contextlib.suppress(UnknownJobError):
                    s = self.store.load_job(jid)["state"]
                    by_state[s] = by_state.get(s, 0) + 1
            return {
                "by_state": by_state,
                "active": sum(by_state.get(s, 0) for s in ACTIVE_STATES),
                "queued": by_state.get(QUEUED, 0),
                "running": self._running_count(),
                "capacity": self.max_jobs,
            }


def registry_summary(store: JobStore) -> dict:
    """Storage-only agent summary for `agent status` when the daemon is down."""
    by_state: dict[str, int] = {}
    for jid in store.job_ids():
        doc = storage.read_json(store.job_path(jid)) or {}
        s = doc.get("state", "unknown")
        by_state[s] = by_state.get(s, 0) + 1
    return {
        "by_state": by_state,
        "active": sum(by_state.get(s, 0) for s in ACTIVE_STATES),
        "queued": by_state.get(QUEUED, 0),
    }
