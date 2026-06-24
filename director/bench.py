"""`director bench` — the experiment that tests the hypothesis (Phase 3 §4).

Runs the SAME task under several profiles and diffs cost / quality / wall-time.
The scientific control that makes the comparison fair: **plan once, run many.**

1. Plan the task a single time (under a chosen `--plan-profile`, default
   `all-frontier`). This produces the DAG and the acceptance tests, committed to
   the plan's job branch. That branch — with its failing tests — is *frozen*.
2. For each profile, branch a fresh job branch off the frozen one (so every
   profile faces byte-for-byte identical acceptance tests), rewrite `plan.json`
   to that branch, reset run state/cost/metrics, and `run` it.

Quality is therefore "did the same acceptance tests pass," isolating the
executor tier as the only independent variable — exactly what the hypothesis is
about. Planning cost is shared (counted once); each profile reports its own run
cost. Each profile's Config is loaded directly from its profile TOML (run_plan
and run_job take a Config object), so the repo's tracked `config.toml` is never
touched — only the working branch is restored at the end.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from director import config, gitutil
from director.cost import CostLedger
from director.models import Plan
from director.plan import run_plan
from director.run import run_job


@dataclass
class BenchRow:
    profile: str
    n_nodes: int = 0
    done: int = 0
    executor_pct: float = 0.0
    escalated: int = 0
    review_pct: float = 0.0
    integration_ok: bool = False
    run_cost: float = 0.0
    wall_secs: float = 0.0
    error: str | None = None


@dataclass
class BenchResult:
    task: str
    plan_profile: str
    plan_cost: float
    job_id: str
    rows: list[BenchRow] = field(default_factory=list)


def _profile_path(fdir: Path, name: str) -> Path:
    p = fdir / "profiles" / f"{name}.toml"
    if not p.exists():
        raise FileNotFoundError(
            f"profile not found: {p}\n"
            f"bench compares config variants — create it by copying your config, e.g.:\n"
            f"  cp .director/config.toml {p}\n"
            f"then edit its executor tier."
        )
    return p


def _reset_run_artifacts(fdir: Path) -> None:
    """Each profile gets a clean ledger/state/metrics so its numbers are its own."""
    for name in ("state.json", "costs.jsonl", "metrics.jsonl"):
        (fdir / name).unlink(missing_ok=True)


def run_bench(
    task: str,
    repo: str,
    profiles: list[str],
    log,
    *,
    plan_profile: str | None = None,
    parallel: int = 1,
    max_attempts: int = 0,
) -> BenchResult:
    repo = Path(repo).resolve()
    fdir = repo / ".director"
    bench_dir = fdir / "bench"
    bench_dir.mkdir(parents=True, exist_ok=True)

    profile_cfgs = {
        p: config.load_file(_profile_path(fdir, p)) for p in profiles
    }  # validate up front
    plan_profile = plan_profile or ("all-frontier" if "all-frontier" in profiles else profiles[0])
    plan_cfg = config.load_file(_profile_path(fdir, plan_profile))

    base_branch = gitutil.current_branch(repo)

    try:
        # --- plan once (frozen acceptance tests) --------------------------------
        (fdir / "plan_stage.json").unlink(missing_ok=True)  # never resume a stale plan
        _reset_run_artifacts(fdir)
        log(f"[bench] planning once under '{plan_profile}' …")
        pres = run_plan(task, str(repo), plan_cfg, log, auto=True, critique=True)
        if pres.paused:
            raise RuntimeError(
                f"planning paused at gate '{pres.stage}' — bench needs an unattended "
                f"plan. (run_plan returned paused under --auto, which shouldn't happen)"
            )
        frozen = Plan.from_json((fdir / "plan.json").read_text())
        plan_cost = CostLedger(fdir / "costs.jsonl").total()
        log(
            f"[bench] plan ready: {len(frozen.nodes)} node(s) on {frozen.job_branch}, "
            f"plan cost ${plan_cost:.4f}"
        )

        result = BenchResult(
            task=task, plan_profile=plan_profile, plan_cost=plan_cost, job_id=frozen.job_id
        )

        # --- run each profile against the frozen plan ---------------------------
        for p in profiles:
            row = BenchRow(profile=p)
            try:
                branch = f"director/bench-{p}-{frozen.job_id}"
                if gitutil.branch_exists(branch, repo):
                    gitutil.checkout(base_branch, repo)
                    gitutil.git(["branch", "-D", branch], repo, check=False)
                # branch off the frozen plan branch → identical failing tests
                gitutil.create_branch(branch, repo, base=frozen.job_branch)

                plan_d = json.loads((fdir / "plan.json").read_text())
                plan_d["job_id"] = f"{frozen.job_id}-{p}"
                plan_d["job_branch"] = branch
                (fdir / "plan.json").write_text(json.dumps(plan_d, indent=2))

                _reset_run_artifacts(fdir)
                cfg_p = profile_cfgs[p]

                log(f"[bench] === profile '{p}' (executor={cfg_p.model_for('executor')}) ===")
                t0 = time.perf_counter()
                rj = run_job(
                    str(repo),
                    cfg_p,
                    parallel=parallel,
                    max_attempts=max_attempts or cfg_p.max_attempts,
                    log=log,
                )
                row.wall_secs = round(time.perf_counter() - t0, 1)
                row.n_nodes = rj["n_nodes"]
                row.done = len(rj["done"])
                row.executor_pct = rj["executor_tier_pct"]
                row.escalated = len(rj["escalated"])
                row.review_pct = rj["stage_two_trigger_rate"]
                row.integration_ok = rj["integration_ok"]
                row.run_cost = rj["cost_total"]
                # keep this profile's metrics stream for the record
                if (fdir / "metrics.jsonl").exists():
                    shutil.copyfile(fdir / "metrics.jsonl", bench_dir / f"{p}.metrics.jsonl")
            except Exception as e:  # one profile failing must not sink the whole bench
                row.error = str(e)[:300]
                log(f"[bench] profile '{p}' errored: {row.error}")
            result.rows.append(row)

        (bench_dir / "summary.json").write_text(json.dumps(_summary_dict(result), indent=2))
        return result
    finally:
        # restore the working branch — but NON-FATALLY: by this point every
        # profile's data is collected and summary.json is written, so a failed
        # checkout (e.g. a target repo that committed its .director runtime files
        # before the .gitignore seed existed) must not sink the whole bench. Log
        # and leave the repo where it is rather than raising over the result.
        try:
            gitutil.checkout(base_branch, repo)
        except Exception as e:
            log(
                f"[bench] WARNING: could not restore branch '{base_branch}' "
                f"({str(e)[:160]}); repo left on the last bench branch."
            )


def _summary_dict(r: BenchResult) -> dict:
    return {
        "task": r.task,
        "plan_profile": r.plan_profile,
        "plan_cost": r.plan_cost,
        "job_id": r.job_id,
        "rows": [row.__dict__ for row in r.rows],
    }


def bench_report(r: BenchResult) -> str:
    lines = [
        "",
        "=" * 78,
        "BENCH — same task & acceptance tests across profiles",
        "=" * 78,
        f"task: {r.task}",
        f"planned once under '{r.plan_profile}'  (shared plan cost ${r.plan_cost:.4f})",
        "",
    ]
    hdr = (
        f"{'profile':16} {'done':>7} {'exec%':>6} {'esc':>4} "
        f"{'rev%':>5} {'integ':>6} {'run $':>9} {'wall':>7}"
    )
    lines += [hdr, "-" * len(hdr)]
    for row in r.rows:
        if row.error:
            lines.append(f"{row.profile:16} ERROR: {row.error}")
            continue
        lines.append(
            f"{row.profile:16} {f'{row.done}/{row.n_nodes}':>7} "
            f"{row.executor_pct:>5.0f}% {row.escalated:>4} "
            f"{row.review_pct:>4.0f}% {('PASS' if row.integration_ok else 'FAIL'):>6} "
            f"${row.run_cost:>7.4f} {row.wall_secs:>6.0f}s"
        )

    # cost-reduction vs the all-frontier baseline, if it was one of the profiles
    base = next((x for x in r.rows if x.profile == "all-frontier" and not x.error), None)
    if base and base.run_cost > 0:
        lines += ["", "run-cost vs all-frontier baseline:"]
        for row in r.rows:
            if row.error or row.profile == "all-frontier":
                continue
            cut = 100 * (1 - row.run_cost / base.run_cost)
            lines.append(
                f"  {row.profile:16} {cut:>5.0f}% cheaper  "
                f"(${row.run_cost:.4f} vs ${base.run_cost:.4f})  "
                f"[target: >80%]"
            )
    lines.append("")
    lines.append("quality = identical acceptance tests; 'integ' is the repo-wide gate.")
    return "\n".join(lines)
