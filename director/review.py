"""Two-stage code review (Phase 2.5) — runs after the deterministic node gate
passes, before merge.

Stage one — spec compliance. The allowlist + test gate (in gates.node_gate) are
the deterministic core and always run. An optional explorer-tier LLM compliance
check (`review.stage_one_llm`) can be layered on; it is advisory (logged, never
merge-blocking) so merge decisions stay deterministic-first.

Stage two — code quality (reviewer tier). COST-GATED: runs only when the node
escalated OR its diff touched more than `review.stage_two_file_threshold` files.
It never runs on the cheap/local executor tier — it uses the `reviewer` tier,
which a profile binds to a strong model (review on a weak model is worthless). A
`critical` finding blocks the merge and re-opens the node.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from director import gitutil
from director.config import Config
from director.gates import build_ignore_matcher
from director.guidance import RepositoryGuidance
from director.models import Node
from director.opencode import run_agent


@dataclass
class ReviewResult:
    stage_two_ran: bool = False
    blocking: bool = False
    summary: str = ""
    detail: str = ""  # feedback fed back to the next attempt if blocking
    calls: list = field(default_factory=list)  # [(role, model, tokens)] for the ledger


def _diff(worktree: Path, timeout: int) -> str:
    """Unified diff of the node's uncommitted work (incl. new files). Staging in
    the worktree's own index is side-effect-free w.r.t. the main repo."""
    gitutil.git(["add", "-A"], worktree, check=False)
    p = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return p.stdout


def _reviewer_message(node: Node, diff: str, stage: str, guidance_context: str = "") -> str:
    return "\n".join(
        [
            f"Perform {stage} review of node '{node.id}' — {node.title}.",
            "",
            "SPEC:",
            node.spec,
            "",
            f"FILE ALLOWLIST: {', '.join(node.files)}",
            "",
            "UNIFIED DIFF (already passed tests + allowlist gate):",
            diff[:20000] if diff else "(empty diff)",
            "",
            guidance_context,
            "",
            "Emit your strict-JSON verdict per your instructions.",
        ]
    )


def _should_run_stage_two(node: Node, worktree: Path, cfg: Config, escalated: bool) -> bool:
    if not cfg.stage_two_enabled:
        return False
    if escalated:
        return True
    # count only real source changes — ignore build byproducts so they don't
    # inflate the file count past the threshold and trip stage two.
    match = build_ignore_matcher(worktree, cfg)
    n_changed = sum(1 for p in gitutil.changed_paths(worktree) if not match(p))
    return n_changed > cfg.stage_two_file_threshold


def review_node(
    node: Node,
    worktree: Path,
    cfg: Config,
    logs: Path,
    log,
    *,
    escalated: bool,
    guidance: RepositoryGuidance | None = None,
) -> ReviewResult:
    from director.plan import _extract_json  # reuse the tolerant JSON extractor

    result = ReviewResult()

    # --- Stage one (advisory LLM compliance, optional) ----------------------
    if cfg.stage_one_llm:
        diff = _diff(worktree, cfg.node_timeout)
        s1 = run_agent(
            agent="reviewer",
            model=cfg.model_for("explorer"),
            message=_reviewer_message(
                node,
                diff,
                "stage-one spec-compliance",
                guidance.for_files([*node.files, *node.tests]) if guidance else "",
            ),
            cwd=worktree,
            log_path=logs / f"{node.id}-review-stage1.jsonl",
            timeout=cfg.node_timeout,
        )
        result.calls.append(("reviewer", cfg.model_for("explorer"), s1.tokens))
        if s1.ok and s1.text.strip():
            log(f"[review] {node.id} stage-one (advisory): {s1.text.strip()[:160]}")

    # --- Stage two (code quality, conditional, blocking) --------------------
    if not _should_run_stage_two(node, worktree, cfg, escalated):
        return result

    result.stage_two_ran = True
    model = cfg.model_for("reviewer")
    why = "escalated" if escalated else f">{cfg.stage_two_file_threshold} files changed"
    log(f"[review] {node.id} stage-two code quality ({model}) — triggered: {why}")
    diff = _diff(worktree, cfg.node_timeout)
    rv = run_agent(
        agent="reviewer",
        model=model,
        message=_reviewer_message(
            node,
            diff,
            "stage-two code-quality",
            guidance.for_files([*node.files, *node.tests]) if guidance else "",
        ),
        cwd=worktree,
        log_path=logs / f"{node.id}-review-stage2.jsonl",
        timeout=cfg.node_timeout,
    )
    result.calls.append(("reviewer", model, rv.tokens))
    if not rv.ok:
        # a failed reviewer call is non-blocking — never let review infra wedge a
        # node that already passed its deterministic gate.
        log(
            f"[review] {node.id} stage-two reviewer call failed "
            f"({rv.error or rv.returncode}); not blocking."
        )
        return result

    try:
        verdict = _extract_json(rv.text)
    except ValueError:
        log(f"[review] {node.id} stage-two returned no JSON verdict; not blocking.")
        return result

    findings = verdict.get("findings", []) or []
    criticals = [f for f in findings if str(f.get("severity", "")).lower() == "critical"]
    result.summary = str(verdict.get("summary", ""))[:200]
    if str(verdict.get("verdict", "")).lower() == "block" or criticals:
        result.blocking = True
        bullet = "\n".join(
            f"- [{f.get('severity')}] {f.get('file', '?')}: {f.get('summary', '')}"
            for f in (criticals or findings)
        )
        result.detail = (
            "Stage-two review BLOCKED this node (critical findings). "
            "Fix these without touching the tests:\n" + bullet
        )
        log(f"[review] {node.id} BLOCKED by stage-two: {len(criticals)} critical finding(s)")
    else:
        log(f"[review] {node.id} stage-two PASS: {result.summary or 'no blocking findings'}")
    return result
