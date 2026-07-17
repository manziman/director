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
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from director import gitutil, proc, setup
from director.config import Config
from director.cost import CostLedger
from director.dag import topo_order, validate
from director.gates import configured_gates
from director.guidance import RepositoryGuidance
from director.jobctx import JobContext
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
    def path(artifact_dir: Path) -> Path:
        return Path(artifact_dir) / "plan_stage.json"

    @classmethod
    def load(cls, artifact_dir: Path) -> PlanProgress | None:
        p = cls.path(artifact_dir)
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text()))

    def save(self, artifact_dir: Path) -> None:
        p = self.path(artifact_dir)
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
def _explorer_prompt(task: str, guidance_context: str = "") -> str:
    return (
        f"Recon for this task — read-only. Produce the relevant-files summary "
        f"per your instructions.\n\nTASK:\n{task}\n\n{guidance_context}"
    )


def _brainstorm_prompt(task: str, summary: str, guidance_context: str = "") -> str:
    return (
        "Produce the design spec for this task per your instructions. Output ONLY "
        "the spec Markdown.\n\n"
        f"TASK:\n{task}\n\n"
        f"REPO RECON SUMMARY:\n{summary}\n\n{guidance_context}\n"
    )


def _spec_critique_prompt(task: str, spec: str, guidance_context: str = "") -> str:
    return (
        "Self-critique pass. Silently re-read the spec below against the ORIGINAL "
        "request and note anything missing, ambiguous, or contradictory. Then output "
        "the REVISED spec that fixes those issues.\n"
        "Output ONLY the final revised spec, in the same Markdown format and starting "
        "at its `# Spec:` heading. Do NOT include your critique notes, a changelog, "
        "or any preamble — the output replaces the spec file verbatim.\n\n"
        f"ORIGINAL REQUEST:\n{task}\n\n"
        f"CURRENT SPEC:\n{spec}\n\n{guidance_context}\n"
    )


def _planner_prompt(spec: str, summary: str, cfg: Config, guidance_context: str = "") -> str:
    gates_ctx = _format_gates_context(cfg)
    return (
        "Decompose the APPROVED SPEC below into a strict-JSON DAG per your "
        "instructions. Build from the spec, not from a raw task. Output ONLY the "
        "JSON object.\n\n"
        f"APPROVED SPEC:\n{spec}\n\n"
        f"REPO RECON SUMMARY:\n{summary}\n\n"
        f"{gates_ctx}\n\n{guidance_context}\n"
    )


def _plan_critique_prompt(
    spec: str, plan_json: str, cfg: Config, guidance_context: str = ""
) -> str:
    gates_ctx = _format_gates_context(cfg)
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
        f"CURRENT PLAN:\n{plan_json}\n\n"
        f"{gates_ctx}\n\n{guidance_context}\n"
    )


def _testauthor_prompt(node: Node, guidance_context: str = "") -> str:
    return (
        "Write acceptance tests for exactly this node, in the listed test file(s), "
        "and nothing else. Confirm they FAIL before implementation exists.\n\n"
        f"NODE: {node.id} — {node.title}\n\n"
        f"SPEC:\n{node.spec}\n\n"
        f"TEST FILE(S) TO CREATE: {', '.join(node.tests)}\n"
        f"IMPLEMENTATION FILES (do NOT create/implement these): {', '.join(node.files)}\n"
        f"The test command will be: {node.test_cmd}\n\n{guidance_context}\n"
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _format_gates_context(cfg: Config) -> str:
    """Format normalized repository gates for planning context.

    Retains declaration order, trims each command, includes only gates with
    non-empty trimmed commands. Returns formatted text with gate name/command
    pairs, or explicit zero-gate statement.
    """
    effective = configured_gates(cfg.gates)

    if not effective:
        return "No repository-wide gates are configured for this project."

    lines = ["REPOSITORY GATES (authoritative complete set):"]
    for gate in effective:
        lines.append(f"  {gate.name}: {gate.command}")
    return "\n".join(lines)


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
    return proc.run_shell(cmd, cwd, timeout=None).returncode


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_plan(data: dict, prog: PlanProgress, workspace: Path) -> Plan:
    nodes = [Node.from_dict(n) for n in data["nodes"]]
    return Plan(
        job_id=prog.job_id,
        task=prog.task,
        repo=str(workspace),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        job_branch=prog.job_branch,
        nodes=nodes,
    )


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def _recon(
    prog: PlanProgress,
    ctx: JobContext,
    cfg: Config,
    ledger: CostLedger,
    logs: Path,
    log,
    guidance: RepositoryGuidance | None = None,
) -> str:
    log(f"[plan] explorer recon ({cfg.model_for('explorer')}) …")
    ex = run_agent(
        agent="explorer",
        model=cfg.model_for("explorer"),
        message=_explorer_prompt(prog.task, guidance.for_planning() if guidance else ""),
        cwd=ctx.workspace,
        log_path=logs / f"{prog.job_id}-explorer.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="explorer", model=cfg.model_for("explorer"), tokens=ex.tokens, cfg=cfg)
    if not ex.ok:
        raise RuntimeError(f"explorer failed: {ex.error or ex.returncode} (see {ex.log_path})")
    summary = ex.text or "(no summary)"
    (ctx.artifact_dir / "recon.md").write_text(summary)
    return summary


def _stage_a_spec(
    prog: PlanProgress,
    ctx: JobContext,
    cfg: Config,
    summary: str,
    ledger: CostLedger,
    logs: Path,
    log,
    guidance: RepositoryGuidance | None = None,
) -> None:
    log(f"[plan] Stage A brainstorm/spec ({cfg.model_for('planner')}) …")
    bs = run_agent(
        agent="brainstorm",
        model=cfg.model_for("planner"),
        message=_brainstorm_prompt(prog.task, summary, guidance.for_planning() if guidance else ""),
        cwd=ctx.workspace,
        log_path=logs / f"{prog.job_id}-brainstorm.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="planner", model=cfg.model_for("planner"), tokens=bs.tokens, cfg=cfg)
    if not bs.ok or not bs.text.strip():
        raise RuntimeError(f"brainstorm failed: {bs.error or bs.returncode} (see {bs.log_path})")
    (ctx.artifact_dir / "spec.md").write_text(bs.text.strip() + "\n")


def _critique_spec(
    prog: PlanProgress,
    ctx: JobContext,
    cfg: Config,
    ledger: CostLedger,
    logs: Path,
    log,
    guidance: RepositoryGuidance | None = None,
) -> None:
    spec = (ctx.artifact_dir / "spec.md").read_text()
    log(f"[plan] --auto: spec self-critique ({cfg.model_for('planner')}) …")
    cr = run_agent(
        agent="brainstorm",
        model=cfg.model_for("planner"),
        message=_spec_critique_prompt(prog.task, spec, guidance.for_planning() if guidance else ""),
        cwd=ctx.workspace,
        log_path=logs / f"{prog.job_id}-spec-critique.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="planner", model=cfg.model_for("planner"), tokens=cr.tokens, cfg=cfg)
    if cr.ok and cr.text.strip():
        (ctx.artifact_dir / "spec.md").write_text(cr.text.strip() + "\n")
        log("[plan] spec revised by self-critique.")


def _author_tests(
    plan: Plan,
    ctx: JobContext,
    cfg: Config,
    ledger: CostLedger,
    logs: Path,
    log,
    *,
    prev_tests: set[str] | None = None,
    guidance: RepositoryGuidance | None = None,
) -> None:
    """Stage C: test-author writes per-node tests, commit, hash, verify red.
    Idempotent — safe to re-run after a plan revision (overwrites test files)."""
    ws = ctx.workspace
    for node in [plan.node(i) for i in topo_order(plan)]:
        log(
            f"[plan] test-author: {node.id} → {', '.join(node.tests)} "
            f"({cfg.model_for('test_author')}) …"
        )
        ta = run_agent(
            agent="test-author",
            model=cfg.model_for("test_author"),
            message=_testauthor_prompt(
                node, guidance.for_files([*node.files, *node.tests]) if guidance else ""
            ),
            cwd=ws,
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
    current_tests = {t for n in plan.nodes for t in n.tests}
    stale = (prev_tests or set()) - current_tests
    for rel in sorted(stale):
        tp = ws / rel
        if tp.exists():
            tp.unlink()
            log(f"[plan] pruned stale test from a prior attempt: {rel}")
    gitutil.commit_all(f"director: acceptance tests for job {plan.job_id}", ws)

    # Hash the committed test files: the node gate refuses to pass if the executor
    # later edits the contract. Captured by director, not the planner.
    for node in plan.nodes:
        node.test_hashes = {}
        for t in node.tests:
            tp = ws / t
            if tp.exists():
                node.test_hashes[t] = _sha256(tp)
    (ctx.artifact_dir / "plan.json").write_text(plan.to_json())

    not_red = [n.id for n in plan.nodes if _run_shell(n.test_cmd, ws) == 0]
    if not_red:
        log(
            f"[plan] WARNING: tests did NOT fail first (not red) for: "
            f"{', '.join(not_red)} — their contract is suspect."
        )


def _stage_bc_decompose(
    prog: PlanProgress,
    ctx: JobContext,
    cfg: Config,
    ledger: CostLedger,
    logs: Path,
    log,
    guidance: RepositoryGuidance | None = None,
) -> Plan:
    fdir = ctx.artifact_dir
    summary = (fdir / "recon.md").read_text() if (fdir / "recon.md").exists() else "(no recon)"
    spec = (fdir / "spec.md").read_text()

    log(f"[plan] Stage B decompose ({cfg.model_for('planner')}) …")
    pl = run_agent(
        agent="planner",
        model=cfg.model_for("planner"),
        message=_planner_prompt(spec, summary, cfg, guidance.for_planning() if guidance else ""),
        cwd=ctx.workspace,
        log_path=logs / f"{prog.job_id}-planner.jsonl",
        timeout=cfg.node_timeout,
    )
    ledger.record(role="planner", model=cfg.model_for("planner"), tokens=pl.tokens, cfg=cfg)
    if not pl.ok:
        raise RuntimeError(f"planner failed: {pl.error or pl.returncode} (see {pl.log_path})")

    plan = _build_plan(_extract_json(pl.text), prog, ctx.workspace)
    validate(plan)
    pj = fdir / "plan.json"
    if pj.exists():
        try:
            prev_tests = {t for n in Plan.from_json(pj.read_text()).nodes for t in n.tests}
        except Exception as e:
            log(f"[plan] could not read prior plan.json for pruning: {e}")
            prev_tests = set()
    else:
        prev_tests = set()
    pj.write_text(plan.to_json())
    log(f"[plan] {len(plan.nodes)} nodes: {', '.join(n.id for n in plan.nodes)}")

    _author_tests(plan, ctx, cfg, ledger, logs, log, prev_tests=prev_tests, guidance=guidance)
    return plan


def _critique_plan(
    plan: Plan,
    prog: PlanProgress,
    ctx: JobContext,
    cfg: Config,
    ledger: CostLedger,
    logs: Path,
    log,
    guidance: RepositoryGuidance | None = None,
) -> Plan:
    spec = (ctx.artifact_dir / "spec.md").read_text()
    log(f"[plan] --auto: plan self-critique ({cfg.model_for('planner')}) …")
    cr = run_agent(
        agent="planner",
        model=cfg.model_for("planner"),
        message=_plan_critique_prompt(
            spec, plan.to_json(), cfg, guidance.for_planning() if guidance else ""
        ),
        cwd=ctx.workspace,
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
    revised = _build_plan(data, prog, ctx.workspace)
    validate(revised)
    (ctx.artifact_dir / "plan.json").write_text(revised.to_json())
    prev_tests = {t for n in plan.nodes for t in n.tests}
    _author_tests(revised, ctx, cfg, ledger, logs, log, prev_tests=prev_tests, guidance=guidance)
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
    ctx: JobContext | None = None,
) -> PlanResult:
    ctx = ctx or JobContext.for_repo(repo)
    ws = ctx.workspace
    guidance = RepositoryGuidance.discover(ws)
    fdir = ctx.artifact_dir
    logs = ctx.logs_dir
    setup.ensure_director_gitignore(ws)  # never let `git add -A` commit generated .director files
    ledger = CostLedger(fdir / "costs.jsonl")
    prog = PlanProgress.load(fdir)

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
        if gitutil.current_branch(ws) != prog.job_branch:
            gitutil.checkout(prog.job_branch, ws)
    else:
        if prog is not None and prog.stage != READY:
            raise RuntimeError(
                f"a plan is already in progress at stage '{prog.stage}' "
                f"(job {prog.job_id}). Use `director plan --continue`, or remove "
                f"{PlanProgress.path(fdir)} to start over."
            )
        if not task:
            raise RuntimeError("a task description is required to start a new plan")
        job_id = ctx.job_id or _job_id()
        job_branch = ctx.job_branch or f"director/job-{job_id}"
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
        # Agent-job workspaces arrive already checked out on the job branch (the
        # runner created the worktree from the captured base commit) — skip
        # branch creation in that case.
        if gitutil.current_branch(ws) != job_branch:
            base = gitutil.current_commit(ws)
            if gitutil.branch_exists(job_branch, ws):
                raise RuntimeError(f"branch {job_branch} already exists")
            gitutil.create_branch(job_branch, ws, base)
            gitutil.checkout(job_branch, ws)
        sync_agents(ws, cfg)
        gitutil.commit_all(f"director: scaffold agents for job {job_id}", ws)
        _recon(prog, ctx, cfg, ledger, logs, log, guidance)

    prog.auto, prog.critique = auto, critique
    plan: Plan | None = None

    # advance through stages until a human gate pauses us or we reach READY
    while True:
        if prog.stage == SPEC:
            _stage_a_spec(
                prog,
                ctx,
                cfg,
                (fdir / "recon.md").read_text(),
                ledger,
                logs,
                log,
                guidance,
            )
            prog.stage = GATE_SPEC
            prog.save(fdir)
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
                _critique_spec(prog, ctx, cfg, ledger, logs, log, guidance)
            prog.stage = DECOMPOSE
            prog.save(fdir)
            continue

        if prog.stage == DECOMPOSE:
            plan = _stage_bc_decompose(prog, ctx, cfg, ledger, logs, log, guidance)
            prog.stage = GATE_PLAN
            prog.save(fdir)
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
                plan = _critique_plan(plan, prog, ctx, cfg, ledger, logs, log, guidance)
            prog.stage = READY
            prog.save(fdir)
            continue

        if prog.stage == READY:
            prog.save(fdir)
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
