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
    done = sum(1 for n in plan.nodes if state[n.id].status == "done")
    esc = sum(1 for n in plan.nodes if state[n.id].escalated)
    reviewed = sum(1 for n in plan.nodes if state[n.id].review_stage_two)
    blocked = sum(1 for n in plan.nodes if state[n.id].review_blocks)
    wif_ok = sum(1 for n in plan.nodes if state[n.id].watch_it_fail == "observed")
    flaky = sum(1 for n in plan.nodes if state[n.id].flake_failed)
    lines += [
        "",
        f"{done}/{len(plan.nodes)} done, {esc} escalated, "
        f"{reviewed} stage-two reviewed, {blocked} re-opened by review",
    ]
    if len(plan.nodes):
        no_esc = done - esc
        lines.append(
            f"executor-tier completion (no escalation): "
            f"{no_esc}/{len(plan.nodes)} = {100 * no_esc / len(plan.nodes):.0f}% "
            f"(hypothesis target: >70%)"
        )
        lines.append(
            f"stage-two review trigger rate: "
            f"{reviewed}/{len(plan.nodes)} = {100 * reviewed / len(plan.nodes):.0f}%"
        )
        lines.append(
            f"watch-it-fail observed (red before green): "
            f"{wif_ok}/{len(plan.nodes)}"
            + (f"   ⚠️  {flaky} node(s) hit a flake re-run failure" if flaky else "")
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
