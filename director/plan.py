"""`director plan` — brainstorm → spec → decompose → test-gated DAG.

Phase 2.5 turns planning into a re-entrant pipeline with two artifact-based
approval gates. director writes an artifact and then either pauses (interactive)
or auto-approves (`--auto`); the human and the self-critic are mechanically the
same gate — both read an artifact, decide, and continue.

  Stage 0  job branch + synced agents (so `--agent <role>` resolves correctly)
  recon    explorer (cheap) reads the repo  → .director/recon.md
  Stage A  planner-tier brainstorm/spec     → .director/spec.md      → GATE 1
  Stage B  planner decomposes the SPEC      → .director/plan.json
  Stage C  test-author writes failing tests (committed, hashed)     → GATE 2
  READY    approved; `director run` may execute

Resumption is driven by `.director/plan_stage.json`. `director plan "<task>"`
starts fresh; `director plan --continue` advances the current gate; `--auto`
swaps a planner self-critique into the gate so nothing blocks.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from director import gitutil, opencode, setup
from director.config import Config
from director.cost import CostLedger
from director.dag import topo_order, validate
from director.models import Node, Plan
from director.opencode import run_agent
from director.setup import sync_agents

# Pipeline stages persisted to .director/plan_stage.json. SPEC/DECOMPOSE are
# transient (executed then advanced in one invocation); GATE_SPEC/GATE_PLAN/READY
# are the points where an invocation can stop.
SPEC, GATE_SPEC, DECOMPOSE, GATE_PLAN, READY = (
    "spec",
    "gate_spec",
    "decompose",
    "gate_plan",
    "ready",
)


@dataclass
class PlanProgress:
    job_id: str
    task: str
    job_branch: str
    stage: str
    auto: bool
    critique: bool

    @staticmethod
    def path(repo: Path) -> Path:
        return Path(repo) / ".director" / "plan_stage.json"

    @classmethod
    def load(cls, repo: Path) -> PlanProgress | None:
        p = cls.path(repo)
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text()))

    def save(self, repo: Path) -> None:
        p = self.path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))


@dataclass
class PlanResult:
    paused: bool  # True = stopped at a human gate; False = reached READY
    stage: str
    job_id: str
    job_branch: str
    n_nodes: int
    artifact: str  # path the human should review next (when paused)
    message: str


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #
def _explorer_prompt(task: str) -> str:
    return (
        f"Recon for this task — read-only. Produce the relevant-files summary "
        f"per your instructions.\n\nTASK:\n{task}"
    )


def _brainstorm_prompt(task: str, summary: str) -> str:
    return (
        "Produce the design spec for this task per your instructions. Output ONLY "
        "the spec Markdown.\n\n"
        f"TASK:\n{task}\n\n"
        f"REPO RECON SUMMARY:\n{summary}\n"
    )


def _spec_critique_prompt(task: str, spec: str) -> str:
    return (
        "Self-critique pass. Silently re-read the spec below against the ORIGINAL "
        "request and note anything missing, ambiguous, or contradictory. Then output "
        "the REVISED spec that fixes those issues.\n"
        "Output ONLY the final revised spec, in the same Markdown format and starting "
        "at its `# Spec:` heading. Do NOT include your critique notes, a changelog, "
        "or any preamble — the output replaces the spec file verbatim.\n\n"
        f"ORIGINAL REQUEST:\n{task}\n\n"
        f"CURRENT SPEC:\n{spec}\n"
    )


def _planner_prompt(spec: str, summary: str) -> str:
    return (
        "Decompose the APPROVED SPEC below into a strict-JSON DAG per your "
        "instructions. Build from the spec, not from a raw task. Output ONLY the "
        "JSON object.\n\n"
        f"APPROVED SPEC:\n{spec}\n\n"
        f"REPO RECON SUMMARY:\n{summary}\n"
    )


def _plan_critique_prompt(spec: str, plan_json: str) -> str:
    return (
        "Self-critique pass on your own DAG. Re-read the plan below against the "
        "approved spec: are any acceptance criteria unaddressed? any node "
        "under-specified for a junior engineer? any two independent nodes sharing "
        "a file? \n"
        "Respond with a SINGLE strict-JSON object and nothing else:\n"
        '  {"revised": false}  — if the plan already covers the spec, OR\n'
        '  {"revised": true, "nodes": [ ...full revised node list... ]}\n'
        "When revising, emit the COMPLETE node list (same schema as before), not a diff.\n\n"
        f"APPROVED SPEC:\n{spec}\n\n"
        f"CURRENT PLAN:\n{plan_json}\n"
    )


def _testauthor_prompt(node: Node) -> str:
    return (
        "Write acceptance tests for exactly this node, in the listed test file(s), "
        "and nothing else. Confirm they FAIL before implementation exists.\n\n"
        f"NODE: {node.id} — {node.title}\n\n"
        f"SPEC:\n{node.spec}\n\n"
        f"TEST FILE(S) TO CREATE: {', '.join(node.tests)}\n"
        f"IMPLEMENTATION FILES (do NOT create/implement these): {', '.join(node.files)}\n"
        f"The test command will be: {node.test_cmd}\n"
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a reply, tolerating code fences or stray prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("agent did not return parseable JSON")


def _run_shell(cmd: str, cwd: Path) -> int:
    import os

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}  # keep the worktree clean
    return subprocess.run(
        cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, env=env
    ).returncode


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_plan(data: dict, prog: PlanProgress, repo: Path) -> Plan:
    nodes = [Node.from_dict(n) for n in data["nodes"]]
    return Plan(
        job_id=prog.job_id,
        task=prog.task,
        repo=str(repo),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        job_branch=prog.job_branch,
        nodes=nodes,
    )


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def _recon(prog: PlanProgress, repo: Path, cfg: Config, ledger: CostLedger, logs: Path, log) -> str:
    log(f"[plan] explorer recon ({cfg.model_for('explorer')}) …")
    ex = run_agent(
        agent="explorer",
        model=cfg.model_for("explorer"),
        message=_explorer_prompt(prog.task),
        cwd=repo,
        log_path=logs / f"{prog.job_id}-explorer.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="explorer", model=cfg.model_for("explorer"), tokens=ex.tokens, cfg=cfg)
    if not ex.ok:
        raise RuntimeError(f"explorer failed: {ex.error or ex.returncode} (see {ex.log_path})")
    summary = ex.text or "(no summary)"
    (repo / ".director" / "recon.md").write_text(summary)
    return summary


def _stage_a_spec(
    prog: PlanProgress, repo: Path, cfg: Config, summary: str, ledger: CostLedger, logs: Path, log
) -> None:
    log(f"[plan] Stage A brainstorm/spec ({cfg.model_for('planner')}) …")
    bs = run_agent(
        agent="brainstorm",
        model=cfg.model_for("planner"),
        message=_brainstorm_prompt(prog.task, summary),
        cwd=repo,
        log_path=logs / f"{prog.job_id}-brainstorm.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="planner", model=cfg.model_for("planner"), tokens=bs.tokens, cfg=cfg)
    if not bs.ok or not bs.text.strip():
        raise RuntimeError(f"brainstorm failed: {bs.error or bs.returncode} (see {bs.log_path})")
    (repo / ".director" / "spec.md").write_text(bs.text.strip() + "\n")


def _critique_spec(
    prog: PlanProgress, repo: Path, cfg: Config, ledger: CostLedger, logs: Path, log
) -> None:
    spec = (repo / ".director" / "spec.md").read_text()
    log(f"[plan] --auto: spec self-critique ({cfg.model_for('planner')}) …")
    cr = run_agent(
        agent="brainstorm",
        model=cfg.model_for("planner"),
        message=_spec_critique_prompt(prog.task, spec),
        cwd=repo,
        log_path=logs / f"{prog.job_id}-spec-critique.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="planner", model=cfg.model_for("planner"), tokens=cr.tokens, cfg=cfg)
    if cr.ok and cr.text.strip():
        (repo / ".director" / "spec.md").write_text(cr.text.strip() + "\n")
        log("[plan] spec revised by self-critique.")


def _author_tests(plan: Plan, repo: Path, cfg: Config, ledger: CostLedger, logs: Path, log) -> None:
    """Stage C: test-author writes per-node tests, commit, hash, verify red.
    Idempotent — safe to re-run after a plan revision (overwrites test files)."""
    for node in [plan.node(i) for i in topo_order(plan)]:
        log(
            f"[plan] test-author: {node.id} → {', '.join(node.tests)} "
            f"({cfg.model_for('test_author')}) …"
        )
        ta = run_agent(
            agent="test-author",
            model=cfg.model_for("test_author"),
            message=_testauthor_prompt(node),
            cwd=repo,
            log_path=logs / f"{plan.job_id}-tests-{node.id}.jsonl",
            timeout=cfg.node_timeout,
        )
        ledger.record(
            role="test_author",
            model=cfg.model_for("test_author"),
            tokens=ta.tokens,
            cfg=cfg,
            node=node.id,
        )
        if not ta.ok:
            raise RuntimeError(f"test-author failed on {node.id}: {ta.error or ta.returncode}")
    gitutil.commit_all(f"director: acceptance tests for job {plan.job_id}", repo)

    # Hash the committed test files: the node gate refuses to pass if the executor
    # later edits the contract. Captured by director, not the planner.
    for node in plan.nodes:
        node.test_hashes = {}
        for t in node.tests:
            tp = repo / t
            if tp.exists():
                node.test_hashes[t] = _sha256(tp)
    (repo / ".director" / "plan.json").write_text(plan.to_json())

    not_red = [n.id for n in plan.nodes if _run_shell(n.test_cmd, repo) == 0]
    if not_red:
        log(
            f"[plan] WARNING: tests did NOT fail first (not red) for: "
            f"{', '.join(not_red)} — their contract is suspect."
        )


def _stage_bc_decompose(
    prog: PlanProgress, repo: Path, cfg: Config, ledger: CostLedger, logs: Path, log
) -> Plan:
    summary = (
        (repo / ".director" / "recon.md").read_text()
        if (repo / ".director" / "recon.md").exists()
        else "(no recon)"
    )
    spec = (repo / ".director" / "spec.md").read_text()

    log(f"[plan] Stage B decompose ({cfg.model_for('planner')}) …")
    pl = run_agent(
        agent="planner",
        model=cfg.model_for("planner"),
        message=_planner_prompt(spec, summary),
        cwd=repo,
        log_path=logs / f"{prog.job_id}-planner.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="planner", model=cfg.model_for("planner"), tokens=pl.tokens, cfg=cfg)
    if not pl.ok:
        raise RuntimeError(f"planner failed: {pl.error or pl.returncode} (see {pl.log_path})")

    plan = _build_plan(_extract_json(pl.text), prog, repo)
    validate(plan)
    (repo / ".director" / "plan.json").write_text(plan.to_json())
    log(f"[plan] {len(plan.nodes)} nodes: {', '.join(n.id for n in plan.nodes)}")

    _author_tests(plan, repo, cfg, ledger, logs, log)
    return plan


def _critique_plan(
    plan: Plan, prog: PlanProgress, repo: Path, cfg: Config, ledger: CostLedger, logs: Path, log
) -> Plan:
    spec = (repo / ".director" / "spec.md").read_text()
    log(f"[plan] --auto: plan self-critique ({cfg.model_for('planner')}) …")
    cr = run_agent(
        agent="planner",
        model=cfg.model_for("planner"),
        message=_plan_critique_prompt(spec, plan.to_json()),
        cwd=repo,
        log_path=logs / f"{prog.job_id}-plan-critique.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="planner", model=cfg.model_for("planner"), tokens=cr.tokens, cfg=cfg)
    if not cr.ok:
        log("[plan] plan self-critique failed; keeping the original plan.")
        return plan
    try:
        data = _extract_json(cr.text)
    except ValueError:
        log("[plan] plan self-critique returned no JSON; keeping the original plan.")
        return plan
    if not data.get("revised"):
        log("[plan] self-critique: plan already covers the spec.")
        return plan

    log("[plan] self-critique revised the DAG; re-authoring tests for the new plan.")
    revised = _build_plan(data, prog, repo)
    validate(revised)
    (repo / ".director" / "plan.json").write_text(revised.to_json())
    _author_tests(revised, repo, cfg, ledger, logs, log)
    return revised


# --------------------------------------------------------------------------- #
# the re-entrant driver
# --------------------------------------------------------------------------- #
def run_plan(
    task: str | None,
    repo: str,
    cfg: Config,
    log,
    *,
    auto: bool = False,
    critique: bool = True,
    cont: bool = False,
) -> PlanResult:
    repo = Path(repo).resolve()
    opencode.set_runtime(dict(cfg.runtime))
    fdir = repo / ".director"
    logs = fdir / "logs"
    setup.ensure_director_gitignore(repo)  # never let `git add -A` commit .director runtime files
    ledger = CostLedger(fdir / "costs.jsonl")
    prog = PlanProgress.load(repo)

    if cont:
        if prog is None:
            raise RuntimeError(
                'nothing to continue: no plan in progress (run `director plan "<task>"` first)'
            )
        # human approval advances the current gate
        if prog.stage == GATE_SPEC:
            prog.stage = DECOMPOSE
        elif prog.stage == GATE_PLAN:
            prog.stage = READY
        elif prog.stage == READY:
            log("[plan] already approved and ready — run `director run`.")
        # carry the flags chosen at start; --auto/--no-critique on --continue may override
        auto = auto or prog.auto
        critique = prog.critique if not auto else critique
        if gitutil.current_branch(repo) != prog.job_branch:
            gitutil.checkout(prog.job_branch, repo)
    else:
        if prog is not None and prog.stage != READY:
            raise RuntimeError(
                f"a plan is already in progress at stage '{prog.stage}' "
                f"(job {prog.job_id}). Use `director plan --continue`, or remove "
                f"{PlanProgress.path(repo)} to start over."
            )
        if not task:
            raise RuntimeError("a task description is required to start a new plan")
        job_id = _job_id()
        job_branch = f"director/job-{job_id}"
        prog = PlanProgress(
            job_id=job_id,
            task=task,
            job_branch=job_branch,
            stage=SPEC,
            auto=auto,
            critique=critique,
        )
        # Stage 0: job branch + agents BEFORE any agent call, so `--agent <role>`
        # resolves the synced role prompt instead of falling back to the default.
        base = gitutil.current_commit(repo)
        if gitutil.branch_exists(job_branch, repo):
            raise RuntimeError(f"branch {job_branch} already exists")
        gitutil.create_branch(job_branch, repo, base)
        gitutil.checkout(job_branch, repo)
        sync_agents(repo)
        gitutil.commit_all(f"director: scaffold agents for job {job_id}", repo)
        _recon(prog, repo, cfg, ledger, logs, log)

    prog.auto, prog.critique = auto, critique
    plan: Plan | None = None

    # advance through stages until a human gate pauses us or we reach READY
    while True:
        if prog.stage == SPEC:
            _stage_a_spec(
                prog, repo, cfg, (repo / ".director" / "recon.md").read_text(), ledger, logs, log
            )
            prog.stage = GATE_SPEC
            prog.save(repo)
            if not auto:
                return _paused(
                    prog,
                    fdir,
                    "spec.md",
                    ledger,
                    "Stage A complete. Review/edit .director/spec.md, then "
                    "`director plan --continue`.",
                )
            if critique:
                _critique_spec(prog, repo, cfg, ledger, logs, log)
            prog.stage = DECOMPOSE
            prog.save(repo)
            continue

        if prog.stage == DECOMPOSE:
            plan = _stage_bc_decompose(prog, repo, cfg, ledger, logs, log)
            prog.stage = GATE_PLAN
            prog.save(repo)
            if not auto:
                return _paused(
                    prog,
                    fdir,
                    "plan.json",
                    ledger,
                    f"Stages B+C complete: {len(plan.nodes)} nodes, tests "
                    f"committed (red). Review .director/plan.json + the test "
                    f"files, then `director plan --continue` to enable `run`.",
                )
            if critique:
                plan = _critique_plan(plan, prog, repo, cfg, ledger, logs, log)
            prog.stage = READY
            prog.save(repo)
            continue

        if prog.stage == READY:
            prog.save(repo)
            if plan is None:
                plan = Plan.from_json((fdir / "plan.json").read_text())
            log(
                f"[plan] READY. job={prog.job_id} branch={prog.job_branch} "
                f"nodes={len(plan.nodes)} plan-cost=${ledger.total():.4f}"
            )
            return PlanResult(
                False,
                READY,
                prog.job_id,
                prog.job_branch,
                len(plan.nodes),
                str(fdir / "plan.json"),
                "Plan approved. Next: `director run`.",
            )


def _paused(
    prog: PlanProgress, fdir: Path, artifact: str, ledger: CostLedger, message: str
) -> PlanResult:
    n_nodes = 0
    pj = fdir / "plan.json"
    if pj.exists():
        try:
            n_nodes = len(Plan.from_json(pj.read_text()).nodes)
        except Exception:
            n_nodes = 0
    return PlanResult(
        True,
        prog.stage,
        prog.job_id,
        prog.job_branch,
        n_nodes,
        str(fdir / artifact),
        message + f"  (plan-cost so far: ${ledger.total():.4f})",
    )
