"""Shared job storage for the Director agent.

Everything the agent knows lives as plain files under a user-level root
(default `~/.director/agent/`, override with `DIRECTOR_AGENT_HOME`):

    agent.json            runtime info written by `director agent serve`
    token                 random bearer token for mutating API calls (0600)
    agent.env             optional strict KEY=VALUE service environment
    jobs/<job-id>/
      request.json        the durable submission (idempotency source of truth)
      job.json            registry record: state, timestamps, pid, settings
      result.json         terminal result, written atomically by the runner
      heartbeat           {"pid": N, "ts": ...} rewritten while the runner lives
      events.jsonl        seq-numbered lifecycle events (monotonic cursor)
      artifacts/          plan_stage/plan/state/costs/metrics/recon/spec/logs
      workspace/          the job's top-level git worktree (job branch checkout)
      worktrees/          per-node worktrees

Both the supervisor and each runner write here; every JSON write is
temp-then-replace so a concurrent reader (CLI, dashboard, another process)
never observes a torn file, and events.jsonl appends take an flock so two
writers can't mint the same sequence number. Read helpers are tolerant of
missing/partial files — read commands must stay useful while a job is being
written and after the agent restarts.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1

# Job lifecycle states (stable strings — part of the CLI contract).
QUEUED = "queued"
PREPARING = "preparing"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
LOST = "lost"

STATES = (QUEUED, PREPARING, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, LOST)
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, LOST})
ACTIVE_STATES = frozenset({QUEUED, PREPARING, RUNNING, INTERRUPTED})

# A runner heartbeats every HEARTBEAT_SECS; a heartbeat older than STALE_SECS
# means the runner is dead or wedged. A live-looking PID alone is never proof
# of liveness (PIDs get recycled) — liveness requires PID *and* fresh heartbeat.
HEARTBEAT_SECS = 5
STALE_SECS = 30


class UnknownJobError(KeyError):
    """No job with that id exists in the registry."""


class RequestConflictError(ValueError):
    """A --request-id was reused with different request content."""


def agent_home() -> Path:
    env = os.environ.get("DIRECTOR_AGENT_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".director" / "agent"


def utc_now() -> str:
    """UTC RFC 3339 with second precision — the timestamp format of the contract."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_job_id() -> str:
    """Globally unique, sortable-by-submission job id."""
    return f"job-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(3)}"


def job_branch_for(job_id: str) -> str:
    return f"director/{job_id}"


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    """A missing or torn JSON file reads as None, never an exception."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def pid_exists(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # NEVER os.kill(pid, 0) here: on Windows signal 0 is CTRL_C_EVENT and
        # would interrupt the whole console group (including us). Query instead.
        import subprocess

        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            check=False,
        )
        return f'"{pid}"' in (res.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def ensure_token(root: Path) -> str:
    """The bearer token gating mutating API calls; user-only permissions."""
    path = Path(root) / "token"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


# In-process serialization for event appends. flock (below) covers cross-process
# writers on POSIX; this lock covers threads everywhere (and is all Windows gets).
_EVENTS_THREAD_LOCK = threading.Lock()


def _flock(f):
    """Exclusive lock on an open file where the platform supports it."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback
        return contextlib.nullcontext()

    class _Lock:
        def __enter__(self):
            fcntl.flock(f, fcntl.LOCK_EX)

        def __exit__(self, *a):
            fcntl.flock(f, fcntl.LOCK_UN)

    return _Lock()


class JobStore:
    """Filesystem-backed job registry. Safe for concurrent multi-process use."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else agent_home()
        self.jobs_root = self.root / "jobs"

    # ---- per-job paths ------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        # Job ids are minted by new_job_id(); anything path-like is rejected so
        # a client-supplied id can never escape the jobs root.
        if "/" in job_id or "\\" in job_id or ".." in job_id or not job_id.startswith("job-"):
            raise UnknownJobError(job_id)
        return self.jobs_root / job_id

    def request_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "request.json"

    def job_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def result_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "result.json"

    def heartbeat_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "heartbeat"

    def events_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "events.jsonl"

    def artifacts_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "artifacts"

    def workspace_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "workspace"

    def worktrees_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "worktrees"

    # ---- registry -----------------------------------------------------------
    def job_ids(self) -> list[str]:
        if not self.jobs_root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.jobs_root.iterdir()
            if p.name.startswith("job-") and (p / "job.json").exists()
        )

    def create_job(self, request: dict) -> dict:
        """Durably accept a submission: write request.json + a queued job.json.

        `request` must already carry resolved repo identity and base commit —
        acceptance means the job can run even if the submitted checkout moves on.
        """
        job_id = request["job_id"]
        jdir = self.job_dir(job_id)
        (jdir / "artifacts").mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.request_path(job_id), request)
        now = utc_now()
        job = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "request_id": request.get("request_id"),
            "labels": request.get("labels", {}),
            "state": QUEUED,
            "reason": None,
            "error": None,
            "repo": request["repo"],
            "git_common_dir": request.get("git_common_dir"),
            "base_ref": request.get("base_ref"),
            "base_commit": request["base_commit"],
            "job_branch": request["job_branch"],
            "workspace": str(self.workspace_dir(job_id)),
            "artifact_dir": str(self.artifacts_dir(job_id)),
            "settings": request.get("settings", {}),
            "submitted_at": request.get("submitted_at", now),
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
            "pid": None,
            "resume_count": 0,
            "exit_code": None,
        }
        atomic_write_json(self.job_path(job_id), job)
        self.append_event(job_id, "submitted", {"state": QUEUED})
        return job

    def load_job(self, job_id: str) -> dict:
        doc = read_json(self.job_path(job_id))
        if doc is None:
            raise UnknownJobError(job_id)
        return doc

    def update_job(self, job_id: str, **fields) -> dict:
        job = self.load_job(job_id)
        job.update(fields)
        job["updated_at"] = utc_now()
        atomic_write_json(self.job_path(job_id), job)
        return job

    def set_state(self, job_id: str, state: str, *, reason: str | None = None, **fields) -> dict:
        assert state in STATES, state
        stamps = {}
        if state == RUNNING and not self.load_job(job_id).get("started_at"):
            stamps["started_at"] = utc_now()  # a resume keeps the original start
        if state in TERMINAL_STATES:
            stamps["completed_at"] = utc_now()
        job = self.update_job(job_id, state=state, reason=reason, **stamps, **fields)
        self.append_event(job_id, "state", {"state": state, "reason": reason})
        return job

    def find_by_request_id(self, request_id: str) -> dict | None:
        for job_id in self.job_ids():
            req = read_json(self.request_path(job_id))
            if req and req.get("request_id") == request_id:
                return req
        return None

    # ---- events -------------------------------------------------------------
    def append_event(self, job_id: str, event: str, data: dict | None = None) -> dict:
        """Append one event with the next monotonic seq. flock serializes
        supervisor and runner appends so cursors stay duplicate-free."""
        path = self.events_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _EVENTS_THREAD_LOCK, open(path, "a+", encoding="utf-8") as f, _flock(f):
            f.seek(0)
            last = 0
            for line in f:
                with contextlib.suppress(ValueError):
                    last = int(json.loads(line).get("seq", last))
            record = {"seq": last + 1, "ts": utc_now(), "event": event, "data": data or {}}
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(record, sort_keys=True) + "\n")
            f.flush()  # the write must be visible BEFORE the flock releases
        return record

    def read_events(self, job_id: str, after: int = 0) -> list[dict]:
        if not self.job_path(job_id).exists():
            raise UnknownJobError(job_id)
        path = self.events_path(job_id)
        out: list[dict] = []
        if not path.exists():
            return out
        for line in path.read_text().splitlines():
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if int(rec.get("seq", 0)) > after:
                    out.append(rec)
        return out

    # ---- heartbeat / result -------------------------------------------------
    def beat(self, job_id: str, pid: int) -> None:
        atomic_write_json(self.heartbeat_path(job_id), {"pid": pid, "ts": utc_now()})

    def heartbeat_age(self, job_id: str) -> float | None:
        try:
            return max(0.0, time.time() - self.heartbeat_path(job_id).stat().st_mtime)
        except OSError:
            return None

    def runner_alive(self, job: dict) -> bool:
        """PID and heartbeat must BOTH check out — a recycled PID with a stale
        heartbeat is not a live runner."""
        pid = job.get("pid")
        if not pid or not pid_exists(int(pid)):
            return False
        age = self.heartbeat_age(job["job_id"])
        return age is not None and age < STALE_SECS

    def write_result(self, job_id: str, result: dict) -> None:
        atomic_write_json(self.result_path(job_id), {"schema_version": SCHEMA_VERSION, **result})

    def read_result(self, job_id: str) -> dict | None:
        return read_json(self.result_path(job_id))


# ---- derived job view (the `show --json` document) --------------------------


def _artifact_summary(artifacts: Path) -> dict:
    """Planning stage, DAG counts, cost, and artifact availability, assembled
    tolerantly from the job's artifact directory."""
    plan_stage = read_json(artifacts / "plan_stage.json")
    plan = read_json(artifacts / "plan.json")
    state = read_json(artifacts / "state.json")

    counts: dict[str, int] = {}
    active: list[str] = []
    n_nodes = 0
    if plan:
        nodes = plan.get("nodes", [])
        n_nodes = len(nodes)
        node_states = (state or {}).get("nodes", {})
        for n in nodes:
            s = node_states.get(n.get("id"), {})
            status = s.get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
            if status == "running":
                active.append(n.get("id"))

    cost_total = 0.0
    with contextlib.suppress(OSError):
        for line in (artifacts / "costs.jsonl").read_text().splitlines():
            with contextlib.suppress(ValueError, TypeError):
                cost_total += float(json.loads(line).get("cost", 0.0))

    stage = (plan_stage or {}).get("stage")
    return {
        "planning": {
            "stage": stage,
            "awaiting_gate": {"gate_spec": "spec", "gate_plan": "plan"}.get(stage),
        },
        "dag": {"total": n_nodes, "by_status": counts, "active": active},
        "cost_total": round(cost_total, 6),
        "artifacts": {
            name: (artifacts / fname).exists()
            for name, fname in (
                ("recon", "recon.md"),
                ("spec", "spec.md"),
                ("plan", "plan.json"),
                ("state", "state.json"),
                ("logs", "logs"),
            )
        },
    }


def _recommended_action(job: dict, alive: bool) -> tuple[str, str]:
    """(message, recommended_action) — enough for an orchestrator to pick the
    next step without scraping logs."""
    state = job["state"]
    if state == QUEUED:
        return ("waiting for a runner slot", "wait")
    if state in (PREPARING, RUNNING):
        if alive:
            return (f"job is {state}", "wait")
        return (
            "runner is not alive but no terminal result exists; restart the agent to reconcile",
            "restart_agent",
        )
    if state == INTERRUPTED:
        return ("job was interrupted; the agent resumes it automatically", "wait_or_retry")
    if state == SUCCEEDED:
        return (f"job succeeded; branch {job.get('job_branch')} is ready", "inspect_branch")
    if state == CANCELLED:
        return ("job was cancelled", "none")
    if state == LOST:
        return ("job state is unrecoverable", "resubmit")
    err = (job.get("error") or {}).get("message") or job.get("reason") or "failed"
    return (f"job failed: {err}", "inspect_logs")


def describe_job(store: JobStore, job_id: str) -> dict:
    """The stable machine-readable job document behind `show --json` (and each
    entry of `list --json`). Works with the daemon down and after restarts."""
    job = store.load_job(job_id)
    result = store.read_result(job_id)
    alive = store.runner_alive(job)
    doc = dict(job)
    request = read_json(store.request_path(job_id)) or {}
    doc["task"] = (request.get("task") or "")[:300]
    doc["runner_alive"] = alive
    doc["heartbeat_age_secs"] = store.heartbeat_age(job_id)
    doc["result"] = result
    doc.update(_artifact_summary(store.artifacts_dir(job_id)))
    message, action = _recommended_action(job, alive)
    doc["message"] = message
    doc["recommended_action"] = action
    return doc
