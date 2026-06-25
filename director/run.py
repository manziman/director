"""`director run` — execute the DAG.

Each node runs in an isolated git worktree on its own task branch. The executor
tier gets up to `max_attempts`, with the failing gate output fed back each time
(fresh OpenCode context per attempt — only the worktree's files and the feedback
carry over). On exhaustion the SAME node is retried once at the escalation tier
(never escalate the whole job). A passing node merges into the job branch.

Independent nodes may run in parallel (`--parallel N`); the DAG guarantees their
allowlists are disjoint, so their merges never conflict. Git mutations
(worktree add/remove, merge) are serialized; model calls run concurrently.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from director import dag, gitutil, setup
from director.config import Config
from director.cost import CostLedger, cost_of
from director.gates import GateResult, integration_gate, node_gate
from director.metrics import MetricsWriter
from director.models import DONE, ESCALATED, FAILED, RUNNING, Node, Plan
from director.opencode import run_agent, watch_it_fail
from director.review import review_node
from director.state import RunState


class CostCeilingExceeded(RuntimeError):
    pass


@dataclass
class NodeOutcome:
    node_id: str
    ok: bool
    tier: str | None  # tier that passed: "executor" | "escalation"
    escalated: bool
    attempts: int
    model: str | None
    tokens: dict  # summed across all calls (for state display)
    calls: list = field(default_factory=list)  # [(tier, model, tokens)] for the ledger
    error: str | None = None
    worktree: Path | None = None
    review_stage_two: bool = False  # did stage-two code review run on any attempt?
    review_blocks: int = 0  # attempts re-opened by a critical review finding
    review_summary: str | None = None
    # Phase 3 measurement
    wall_secs: float = 0.0  # wall time for the whole node (worktree → merge-ready)
    watch_it_fail: dict = field(
        default_factory=dict
    )  # {verdict, ran_before_edit, observed_failure}
    flake_failed: bool = False  # a flake re-run failed this node on some attempt


def _executor_message(node: Node, worktree: Path, feedback: str) -> str:
    parts = [f"Implement node '{node.id}' — {node.title}.", "", "SPEC:", node.spec, ""]
    parts.append("FILES YOU MAY EDIT (allowlist — touch nothing else):")
    for f in node.files:
        fp = worktree / f
        contents = fp.read_text() if fp.exists() else "(does not exist yet — create it)"
        parts += [f"--- {f} ---", contents, ""]
    parts += [
        f"GATE (your tests must pass): {node.test_cmd}",
        "",
        "CURRENT FAILING TEST OUTPUT:",
        feedback,
        "",
    ]
    parts.append(
        "Run the gate, implement in the allowlisted files only, and "
        "re-run until it passes. Do not modify the test files."
    )
    return "\n".join(parts)


def _run_shell(cmd: str, cwd: Path, timeout: int) -> str:
    import os

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}  # keep the worktree clean
    p = subprocess.run(
        cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout, env=env
    )
    return p.stdout + p.stderr


def _attempt_tiers(cfg: Config, max_attempts: int) -> list[tuple[str, str]]:
    """Ordered (tier, model): executor ×max_attempts, then escalation ×1."""
    return [("executor", cfg.model_for("executor"))] * max_attempts + [
        ("escalation", cfg.model_for("escalation"))
    ]


def _process_node(
    node: Node, worktree: Path, cfg: Config, logs: Path, max_attempts: int, log
) -> NodeOutcome:
    """Run the attempt/escalation ladder inside an already-created worktree."""
    feedback = _run_shell(node.test_cmd, worktree, cfg.node_timeout)[-3000:]
    tokens_sum = {"input": 0, "output": 0}
    calls: list = []
    attempts = 0
    escalated = False
    review_stage_two = False
    review_blocks = 0
    review_summary: str | None = None
    wif: dict = {}  # watch-it-fail verdict of the implementing attempt
    flake_failed = False

    for i, (tier, model) in enumerate(_attempt_tiers(cfg, max_attempts)):
        if tier == "executor":
            attempts += 1
        else:
            escalated = True
        n = attempts if tier == "executor" else 1
        log(f"[run] {node.id} [{tier}#{n}] {model} …")
        res = run_agent(
            agent="executor",
            model=model,
            message=_executor_message(node, worktree, feedback),
            cwd=worktree,
            log_path=logs / f"{node.id}-{tier}-{i}.jsonl",
            timeout=cfg.node_timeout,
        )
        tokens_sum["input"] += res.tokens.get("input", 0)
        tokens_sum["output"] += res.tokens.get("output", 0)
        calls.append((tier, model, res.tokens))

        # watch-it-fail (Phase 3 §1): did this attempt run the failing tests before
        # its first edit? Advisory metric; the verdict of the attempt that ends up
        # passing is the one we keep.
        attempt_wif = watch_it_fail(res.tool_events, node.test_cmd)

        gate: GateResult = node_gate(node, worktree, cfg)
        if not gate.ok:
            if "flaky tests" in gate.failures:
                flake_failed = True
            reason = res.error or ("timeout" if res.timed_out else "; ".join(gate.failures))
            log(f"[run] {node.id} fail ({reason}) at {tier}")
            feedback = (gate.detail or reason)[-3000:]
            continue

        # deterministic gate passed → two-stage review (cost-gated) before merge
        review = review_node(node, worktree, cfg, logs, log, escalated=escalated)
        calls.extend(review.calls)
        review_stage_two = review_stage_two or review.stage_two_ran
        if review.summary:
            review_summary = review.summary
        if review.blocking:
            review_blocks += 1
            feedback = review.detail[-3000:]
            continue  # a critical finding re-opens the node (counts against attempts)

        wif = attempt_wif.__dict__
        if not attempt_wif.observed:
            log(f"[run] {node.id} watch-it-fail: {attempt_wif.verdict} ({attempt_wif.detail})")
        log(f"[run] {node.id} PASS at {tier} (executor attempts={attempts})")
        return NodeOutcome(
            node.id,
            True,
            tier,
            escalated,
            attempts,
            model,
            tokens_sum,
            calls,
            None,
            worktree,
            review_stage_two,
            review_blocks,
            review_summary,
            watch_it_fail=wif,
            flake_failed=flake_failed,
        )

    return NodeOutcome(
        node.id,
        False,
        None,
        escalated,
        attempts,
        None,
        tokens_sum,
        calls,
        f"exhausted: {feedback[:200]}",
        worktree,
        review_stage_two,
        review_blocks,
        review_summary,
        watch_it_fail=wif,
        flake_failed=flake_failed,
    )


def run_job(repo: str, cfg: Config, parallel: int, max_attempts: int, log) -> dict:
    repo = Path(repo).resolve()
    fdir = repo / ".director"
    setup.ensure_director_gitignore(repo)  # never let `git add -A` commit .director runtime files
    plan = Plan.from_json((fdir / "plan.json").read_text())
    state = RunState.load_or_init(repo, plan)
    ledger = CostLedger(fdir / "costs.jsonl")
    metrics = MetricsWriter(fdir / "metrics.jsonl")
    logs = fdir / "logs"
    run_t0 = time.perf_counter()
    # Worktrees live OUTSIDE the repo tree: a worktree nested inside the repo lets
    # OpenCode resolve the enclosing repo as the project root and leak edits out of
    # the isolated checkout. A sibling temp dir keeps each worktree its own root.
    wt_root = Path(tempfile.gettempdir()) / "director-worktrees" / plan.job_id
    wt_root.mkdir(parents=True, exist_ok=True)
    git_lock = threading.Lock()

    if gitutil.current_branch(repo) != plan.job_branch:
        gitutil.checkout(plan.job_branch, repo)

    dag.validate(plan)
    done = state.done_ids()
    # `finished` = every node in a TERMINAL state (done | failed | escalated). The
    # scheduler keys off this, not `done`: a node that fails must never be
    # re-scheduled, and the loop must end even when not every node succeeded.
    # (Seeded from state so a resumed run doesn't retry already-failed nodes.)
    finished = {nid for nid, ns in state.nodes.items() if ns.status in (DONE, FAILED, ESCALATED)}
    active: set[str] = set()
    log(
        f"[run] job={plan.job_id} branch={plan.job_branch} "
        f"nodes={len(plan.nodes)} done={len(done)} parallel={parallel}"
    )

    def launch(node_id: str) -> NodeOutcome:
        node = plan.node(node_id)
        with git_lock:
            wt = wt_root / node_id
            gitutil.worktree_remove(wt, repo)  # no-op if not registered
            shutil.rmtree(wt, ignore_errors=True)
            # drop any stale registration left by a killed run (dir gone but git
            # still tracks it) so `worktree add` below can't fail with exit 255.
            gitutil.git(["worktree", "prune"], repo, check=False)
            task_branch = f"director/task-{plan.job_id}-{node_id}"
            if gitutil.branch_exists(task_branch, repo):
                gitutil.git(["branch", "-D", task_branch], repo, check=False)
            gitutil.worktree_add(wt, task_branch, plan.job_branch, repo)
            state[node_id].status = RUNNING
            state[node_id].worktree = str(wt)
            state.save()
        node_t0 = time.perf_counter()
        outcome = _process_node(node, wt, cfg, logs, max_attempts, log)
        outcome.wall_secs = round(time.perf_counter() - node_t0, 1)
        return outcome

    aborted = False
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures: dict = {}
        while len(finished) < len(plan.nodes) and not aborted:
            # exclude both running and already-terminal nodes; deps are satisfied
            # only by nodes that actually succeeded (`done`).
            for nid in dag.ready_nodes(plan, done, active | finished):
                if len(futures) >= parallel:
                    break
                active.add(nid)
                futures[pool.submit(launch, nid)] = nid
            if not futures:
                # nothing running and nothing runnable → the rest are blocked by a
                # failed/unsatisfiable dependency. Mark them failed and stop (never
                # spin re-scheduling terminal nodes).
                blocked = [n.id for n in plan.nodes if n.id not in finished]
                if blocked:
                    log(
                        f"[run] cannot proceed — {len(blocked)} node(s) blocked by "
                        f"failed/unsatisfiable deps: {', '.join(blocked)}"
                    )
                    for nid in blocked:
                        ns = state[nid]
                        ns.status = FAILED
                        ns.error = ns.error or "blocked by a failed dependency"
                        finished.add(nid)
                    state.save()
                break
            completed, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for fut in completed:
                nid = futures.pop(fut)
                active.discard(nid)
                outcome = fut.result()
                try:
                    _finalize(outcome, plan, state, repo, git_lock, ledger, cfg, log, metrics)
                except CostCeilingExceeded as e:
                    log(f"[run] ABORT: {e}")
                    aborted = True
                finished.add(nid)  # terminal regardless of pass/fail → never re-scheduled
                if outcome.ok and state[nid].status == DONE:
                    done.add(nid)

    integ = integration_gate(repo, cfg)
    log(
        f"[run] integration gate: "
        f"{'PASS' if integ.ok else 'FAIL (' + ', '.join(integ.failures) + ')'}"
    )

    state.save()
    n = len(plan.nodes)
    done_l = sorted(done)
    escalated_l = [nd.id for nd in plan.nodes if state[nd.id].escalated]
    reviewed_l = [nd.id for nd in plan.nodes if state[nd.id].review_stage_two]
    # executor-tier completion = done without ever escalating (the hypothesis metric)
    exec_done = [nid for nid in done_l if nid not in set(escalated_l)]
    wall = round(time.perf_counter() - run_t0, 1)
    result = {
        "job_id": plan.job_id,
        "done": done_l,
        "failed": [nd.id for nd in plan.nodes if state[nd.id].status == FAILED],
        "escalated": escalated_l,
        "reviewed": reviewed_l,
        "review_blocked": [nd.id for nd in plan.nodes if state[nd.id].review_blocks],
        "integration_ok": integ.ok,
        "integration_detail": integ.detail,
        "cost_total": ledger.total(),
        "by_role": ledger.by_role(),
        "by_model": ledger.by_model(),
        "wall_secs": wall,
        "n_nodes": n,
        "executor_tier_completion": len(exec_done),
        "executor_tier_pct": round(100 * len(exec_done) / n, 1) if n else 0.0,
        "escalation_rate": round(100 * len(escalated_l) / n, 1) if n else 0.0,
        "stage_two_trigger_rate": round(100 * len(reviewed_l) / n, 1) if n else 0.0,
    }
    # run-level metrics record (Phase 3): derived rates + the resolved tier map so a
    # metrics line is self-describing about which models produced it.
    metrics.write(
        {
            "kind": "run",
            "job_id": plan.job_id,
            "tiers": dict(cfg.tiers),
            "n_nodes": n,
            "done": len(done_l),
            "failed": len(result["failed"]),
            "escalated": len(escalated_l),
            "executor_tier_completion": len(exec_done),
            "executor_tier_pct": result["executor_tier_pct"],
            "escalation_rate": result["escalation_rate"],
            "stage_two_trigger_rate": result["stage_two_trigger_rate"],
            "integration_ok": integ.ok,
            "wall_secs": wall,
            "cost_total": ledger.total(),
            "by_role": ledger.by_role(),
            "by_model": ledger.by_model(),
        }
    )
    return result


def _finalize(
    outcome: NodeOutcome,
    plan: Plan,
    state: RunState,
    repo: Path,
    git_lock,
    ledger: CostLedger,
    cfg: Config,
    log,
    metrics: MetricsWriter,
):
    ns = state[outcome.node_id]
    ns.attempts = outcome.attempts
    ns.escalated = outcome.escalated
    ns.tier_used = outcome.tier
    ns.model_used = outcome.model
    ns.tokens = outcome.tokens
    ns.review_stage_two = outcome.review_stage_two
    ns.review_blocks = outcome.review_blocks
    ns.review_summary = outcome.review_summary
    ns.wall_secs = outcome.wall_secs
    ns.watch_it_fail = (outcome.watch_it_fail or {}).get("verdict")
    ns.flake_failed = outcome.flake_failed

    # precise cost: one ledger entry per model call, tagged with its tier/role.
    # Also accumulate a per-node by-role breakdown for the metrics record.
    node_cost = 0.0
    node_by_role: dict[str, dict] = {}
    for tier, model, tokens in outcome.calls:
        node_cost += ledger.record(
            role=tier, model=model, tokens=tokens, cfg=cfg, node=outcome.node_id
        )
        g = node_by_role.setdefault(tier, {"input": 0, "output": 0, "cost": 0.0, "calls": 0})
        g["input"] += int(tokens.get("input", 0))
        g["output"] += int(tokens.get("output", 0))
        g["cost"] += cost_of(model, tokens, cfg)
        g["calls"] += 1
    ns.cost_usd = node_cost

    task_branch = f"director/task-{plan.job_id}-{outcome.node_id}"
    with git_lock:
        if outcome.ok and outcome.worktree:
            gitutil.commit_all(
                f"director: node {outcome.node_id} via {outcome.tier}", outcome.worktree
            )
            merge = gitutil.merge_branch(
                task_branch, repo, message=f"director: merge node {outcome.node_id}"
            )
            if merge.returncode != 0:
                gitutil.git(["merge", "--abort"], repo, check=False)
                ns.status = FAILED
                ns.error = f"merge conflict: {merge.stdout}{merge.stderr}"[:300]
                log(f"[run] {outcome.node_id} MERGE FAILED")
            else:
                ns.status = DONE
        else:
            ns.status = ESCALATED if outcome.escalated else FAILED
            ns.error = outcome.error
        if outcome.worktree and Path(outcome.worktree).exists():
            gitutil.worktree_remove(outcome.worktree, repo)
            shutil.rmtree(outcome.worktree, ignore_errors=True)
        gitutil.git(["branch", "-D", task_branch], repo, check=False)
        state.save()

    node = plan.node(outcome.node_id)
    metrics.write(
        {
            "kind": "node",
            "job_id": plan.job_id,
            "node": outcome.node_id,
            "title": node.title,
            "difficulty": node.estimated_difficulty,
            "status": ns.status,
            "tier_used": outcome.tier,
            "model_used": outcome.model,
            "attempts": outcome.attempts,
            "escalated": outcome.escalated,
            "wall_secs": outcome.wall_secs,
            "tokens": outcome.tokens,
            "cost_usd": round(node_cost, 6),
            "by_role": node_by_role,
            "review_stage_two": outcome.review_stage_two,
            "review_blocks": outcome.review_blocks,
            "watch_it_fail": outcome.watch_it_fail or {"verdict": "unknown"},
            "flake_failed": outcome.flake_failed,
        }
    )

    if cfg.cost_ceiling and ledger.total() > cfg.cost_ceiling:
        raise CostCeilingExceeded(f"cost ${ledger.total():.4f} > ${cfg.cost_ceiling:.2f}")
