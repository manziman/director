"""Human-readable summaries for `director status` and the end of `director run`."""

from __future__ import annotations

from pathlib import Path

from director.models import Plan
from director.state import RunState

_STATUS_GLYPH = {
    "done": "✅",
    "pending": "·",
    "running": "…",
    "escalated": "⚠️ ",
    "failed": "❌",
}


def status_summary(plan: Plan, state: RunState) -> dict:
    """Aggregate status counts shared by `status_table` and the web UI
    (`director ui`), so the two views can't drift."""
    n = len(plan.nodes)
    by_status: dict[str, int] = {}
    for nd in plan.nodes:
        s = state[nd.id].status
        by_status[s] = by_status.get(s, 0) + 1
    done = by_status.get("done", 0)
    esc = sum(1 for nd in plan.nodes if state[nd.id].escalated)
    reviewed = sum(1 for nd in plan.nodes if state[nd.id].review_stage_two)
    no_esc = done - esc
    return {
        "total": n,
        "by_status": by_status,
        "done": done,
        "escalated": esc,
        "stage_two_reviewed": reviewed,
        "review_blocked": sum(1 for nd in plan.nodes if state[nd.id].review_blocks),
        "watch_it_fail_observed": sum(
            1 for nd in plan.nodes if state[nd.id].watch_it_fail == "observed"
        ),
        "flaky": sum(1 for nd in plan.nodes if state[nd.id].flake_failed),
        "executor_tier_completion": no_esc,
        "executor_tier_pct": (100 * no_esc / n) if n else 0.0,
        "stage_two_trigger_pct": (100 * reviewed / n) if n else 0.0,
        "cost_total": sum(state[nd.id].cost_usd for nd in plan.nodes),
    }


def status_table(repo: str) -> str:
    repo = Path(repo).resolve()
    plan_path = repo / ".director" / "plan.json"
    if not plan_path.exists():
        return 'No plan found. Run `director plan "<task>"` first.'
    plan = Plan.from_json(plan_path.read_text())
    state = RunState.load_or_init(repo, plan)

    lines = [f"job {plan.job_id}  ({plan.job_branch})", f"task: {plan.task}", ""]
    lines.append(f"{'node':24} {'status':10} {'tier':10} {'att':>3} {'cost':>9}")
    lines.append("-" * 60)
    for n in plan.nodes:
        s = state[n.id]
        glyph = _STATUS_GLYPH.get(s.status, "?")
        lines.append(
            f"{n.id[:24]:24} {glyph} {s.status:8} "
            f"{(s.tier_used or '-'):10} {s.attempts:>3} ${s.cost_usd:>7.4f}"
        )
    m = status_summary(plan, state)
    lines += [
        "",
        f"{m['done']}/{m['total']} done, {m['escalated']} escalated, "
        f"{m['stage_two_reviewed']} stage-two reviewed, {m['review_blocked']} re-opened by review",
    ]
    if m["total"]:
        lines.append(
            f"executor-tier completion (no escalation): "
            f"{m['executor_tier_completion']}/{m['total']} = {m['executor_tier_pct']:.0f}% "
            f"(hypothesis target: >70%)"
        )
        lines.append(
            f"stage-two review trigger rate: "
            f"{m['stage_two_reviewed']}/{m['total']} = {m['stage_two_trigger_pct']:.0f}%"
        )
        lines.append(
            f"watch-it-fail observed (red before green): "
            f"{m['watch_it_fail_observed']}/{m['total']}"
            + (f"   ⚠️  {m['flaky']} node(s) hit a flake re-run failure" if m["flaky"] else "")
        )
    return "\n".join(lines)


def run_summary(result: dict) -> str:
    lines = ["", "=" * 60, f"RUN SUMMARY — job {result['job_id']}", "=" * 60]
    lines.append(f"done:      {', '.join(result['done']) or '(none)'}")
    if result["escalated"]:
        lines.append(f"escalated: {', '.join(result['escalated'])}")
    if result.get("reviewed"):
        lines.append(f"stage-two reviewed: {', '.join(result['reviewed'])}")
    if result.get("review_blocked"):
        lines.append(f"review re-opened:   {', '.join(result['review_blocked'])}")
    if result["failed"]:
        lines.append(f"FAILED:    {', '.join(result['failed'])}")
    lines.append(f"integration gate: {'PASS' if result['integration_ok'] else 'FAIL'}")
    if not result["integration_ok"] and result.get("integration_detail"):
        lines.append(result["integration_detail"][-1500:])

    if result.get("n_nodes"):
        lines += ["", "measurement:"]
        lines.append(
            f"  executor-tier completion (no escalation): "
            f"{result['executor_tier_completion']}/{result['n_nodes']} = "
            f"{result['executor_tier_pct']:.0f}%  (hypothesis target: >70%)"
        )
        lines.append(f"  escalation rate:          {result['escalation_rate']:.0f}%")
        lines.append(f"  stage-two trigger rate:   {result['stage_two_trigger_rate']:.0f}%")
        lines.append(f"  wall time:                {result['wall_secs']:.0f}s")

    lines += ["", "cost by role:"]
    for role, g in sorted(result["by_role"].items()):
        lines.append(
            f"  {role:12} {g['calls']:>2} calls  "
            f"in={g['input']:>8} out={g['output']:>7}  ${g['cost']:.4f}"
        )
    lines += ["", "cost by resolved model:"]
    for model, g in sorted(result["by_model"].items()):
        lines.append(f"  {model:48} ${g['cost']:.4f}")
    lines.append(f"\nTOTAL: ${result['cost_total']:.4f}")
    return "\n".join(lines)
