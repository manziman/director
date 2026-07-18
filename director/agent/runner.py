"""The per-job runner process (`director agent run-job <job-id>`).

The supervisor spawns one runner per job in its own process group. The runner
— not the supervisor — owns lifecycle truth for its job: it heartbeats while
alive, appends events, and writes the terminal `result.json` atomically, so a
supervisor crash never loses a finished job and a runner crash is detectable
(stale heartbeat, no result).

Execution reuses the ordinary planning/run pipeline through a JobContext:

  workspace   = jobs/<id>/workspace   (a top-level worktree of the source repo,
                                       on branch director/<job-id> from the
                                       captured base commit)
  artifacts   = jobs/<id>/artifacts   (plan/state/costs/metrics/logs)
  node trees  = jobs/<id>/worktrees

The submitted repository's checkout, index, and uncommitted files are never
touched; only the job branch (and per-node task branches) are created in it.
Because plan_stage.json/plan.json/state.json live in the job's artifact dir,
an interrupted job resumes from persisted state when the runner is restarted.
"""

from __future__ import annotations

import os
import threading
import traceback
from pathlib import Path

from director import config, gitutil
from director.agent import storage
from director.agent.storage import JobStore
from director.jobctx import JobContext


class JobError(RuntimeError):
    """A job failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _prepare_workspace(store: JobStore, job: dict, log) -> Path:
    """Create (or re-adopt, on resume) the job's top-level worktree."""
    repo = Path(job["repo"])
    ws = store.workspace_dir(job["job_id"])
    branch = job["job_branch"]
    if (ws / ".git").exists():
        return ws  # resume: the worktree survived the interruption
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise JobError("invalid_repo", f"source repository not found: {repo}")
    ws.parent.mkdir(parents=True, exist_ok=True)
    gitutil.git(["worktree", "prune"], repo, check=False)
    if gitutil.branch_exists(branch, repo):
        # An earlier attempt created the branch but lost the worktree — check
        # the existing branch out rather than recreating it from base.
        res = gitutil.git(["worktree", "add", str(ws), branch], repo, check=False)
    else:
        res = gitutil.git(
            ["worktree", "add", "-b", branch, str(ws), job["base_commit"]], repo, check=False
        )
    if res.returncode != 0:
        raise JobError("worktree_failed", (res.stderr or res.stdout).strip()[:500])
    log(f"[agent:{job['job_id']}] workspace at {ws} on {branch}")
    return ws


def _plan_and_run(store: JobStore, job: dict, ctx: JobContext, log) -> dict:
    from director.cost import CostLedger
    from director.plan import READY, PlanProgress, run_plan
    from director.run import run_job as execute_dag

    job_id = job["job_id"]
    settings = job.get("settings", {})
    request = storage.read_json(store.request_path(job_id)) or {}
    task = request.get("task")

    # The runner NEVER reloads config from the submitted checkout: the resolved
    # snapshot was captured (and validated) at submission, so post-submission
    # edits to the repo's .director/config.toml cannot change an accepted job.
    snap = storage.read_json(store.config_path(job_id))
    if snap is None:
        raise JobError("config_invalid", "job has no persisted config snapshot")
    try:
        cfg = config.from_snapshot(snap, store.config_path(job_id))
    except (FileNotFoundError, ValueError) as e:
        raise JobError("config_invalid", str(e)) from e

    critique = not settings.get("no_critique", False)
    prog = PlanProgress.load(ctx.artifact_dir)
    if prog is not None and prog.stage == READY and (ctx.artifact_dir / "plan.json").exists():
        log(f"[agent:{job_id}] plan already READY — resuming execution")
    else:
        store.append_event(job_id, "planning", {"resumed": prog is not None})
        try:
            result = run_plan(
                task if prog is None else None,
                str(ctx.workspace),
                cfg,
                log,
                auto=True,
                critique=critique,
                cont=prog is not None,
                ctx=ctx,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            raise JobError("planning_failed", str(e)) from e
        if result.stage != READY:
            raise JobError("planning_failed", f"planning stopped at {result.stage}")
        store.append_event(job_id, "planning_ready", {"n_nodes": result.n_nodes})

    max_cost = float(settings.get("max_cost") or 0.0)
    if max_cost > 0:
        spent = CostLedger(ctx.artifact_dir / "costs.jsonl").total()
        if spent > max_cost:
            raise JobError("over_budget", f"spent ${spent:.4f} exceeds max-cost ${max_cost:.2f}")

    try:
        run_result = execute_dag(
            str(ctx.workspace),
            cfg,
            parallel=int(settings.get("parallel") or 1),
            max_attempts=int(settings.get("max_attempts") or 0) or cfg.max_attempts,
            log=log,
            ctx=ctx,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        raise JobError("run_failed", str(e)) from e
    store.append_event(
        job_id,
        "run_finished",
        {
            "integration_ok": run_result["integration_ok"],
            "done": len(run_result["done"]),
            "failed": len(run_result["failed"]),
        },
    )
    return run_result


def run_job_process(job_id: str, root: Path | None = None, log=None) -> int:
    """The runner entry point. Returns the process exit code (0 = job succeeded)."""
    import sys

    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    store = JobStore(root)
    job = store.load_job(job_id)
    pid = os.getpid()

    stop = threading.Event()

    def _beat():
        while not stop.is_set():
            store.beat(job_id, pid)
            stop.wait(storage.HEARTBEAT_SECS)

    store.beat(job_id, pid)
    beater = threading.Thread(target=_beat, daemon=True)
    beater.start()

    try:
        store.set_state(job_id, storage.PREPARING, pid=pid)
        ws = _prepare_workspace(store, job, log)
        ctx = JobContext(
            source_repo=Path(job["repo"]),
            workspace=ws,
            artifact_dir=store.artifacts_dir(job_id),
            node_worktree_dir=store.worktrees_dir(job_id),
            job_id=job_id,
            job_branch=job["job_branch"],
            base_commit=job["base_commit"],
        )
        store.set_state(job_id, storage.RUNNING, pid=pid)
        run_result = _plan_and_run(store, job, ctx, log)

        ok = bool(run_result["integration_ok"]) and not run_result["failed"]
        result = {
            "ok": ok,
            "error": None
            if ok
            else {
                "code": "gates_failed",
                "message": "integration gate failed"
                if not run_result["integration_ok"]
                else f"nodes failed: {', '.join(run_result['failed'])}",
            },
            "run": run_result,
            "finished_at": storage.utc_now(),
        }
        exit_code = 0 if ok else 1
    except JobError as e:
        result = {
            "ok": False,
            "error": {"code": e.code, "message": str(e)},
            "run": None,
            "finished_at": storage.utc_now(),
        }
        exit_code = 1
        log(f"[agent:{job_id}] {e.code}: {e}")
    except Exception as e:  # noqa: BLE001 - a runner must always leave a result behind
        result = {
            "ok": False,
            "error": {"code": "internal", "message": f"{type(e).__name__}: {e}"},
            "run": None,
            "finished_at": storage.utc_now(),
        }
        exit_code = 1
        log(f"[agent:{job_id}] internal error:\n{traceback.format_exc()}")
    finally:
        stop.set()
        beater.join(timeout=2)

    result["exit_code"] = exit_code
    store.write_result(job_id, result)
    state = storage.SUCCEEDED if result["ok"] else storage.FAILED
    reason = None if result["ok"] else (result["error"] or {}).get("code")
    store.set_state(job_id, state, reason=reason, error=result["error"], exit_code=exit_code)
    return exit_code
