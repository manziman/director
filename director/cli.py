"""director CLI — plan | run | status | ui | bench | sync-agents | init."""

from __future__ import annotations

import argparse
import sys

from director import __version__, config
from director.report import run_summary, status_table
from director.setup import sync_agents


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def cmd_plan(args) -> int:
    from director.plan import run_plan

    cfg = config.load(args.repo)
    result = run_plan(
        args.task,
        args.repo,
        cfg,
        _log,
        auto=args.auto,
        critique=not args.no_critique,
        cont=getattr(args, "continue"),
    )
    print(result.message)
    return 0


def cmd_run(args) -> int:
    from director.run import run_job

    cfg = config.load(args.repo)
    result = run_job(
        args.repo,
        cfg,
        parallel=args.parallel,
        max_attempts=args.max_attempts or cfg.max_attempts,
        log=_log,
    )
    print(run_summary(result))
    failed = bool(result["failed"]) or not result["integration_ok"]
    return 1 if failed else 0


def cmd_status(args) -> int:
    print(status_table(args.repo))
    return 0


def cmd_ui(args) -> int:
    from director.web.server import serve

    serve(args.repo, host=args.host, port=args.port, open_browser=args.open, log=_log)
    return 0


def cmd_bench(args) -> int:
    from director.bench import bench_report, run_bench

    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    if not profiles:
        _log("director: error: --profiles is empty")
        return 2
    result = run_bench(
        args.task,
        args.repo,
        profiles,
        _log,
        plan_profile=args.plan_profile,
        parallel=args.parallel,
        max_attempts=args.max_attempts or 0,
    )
    print(bench_report(result))
    return 0


def cmd_sync_agents(args) -> int:
    written = sync_agents(args.repo)
    if not written:
        print("No provider-specific agent files needed (no OpenCode provider configured).")
    else:
        print("Synced:\n  " + "\n  ".join(written))
    return 0


def cmd_init(args) -> int:
    from pathlib import Path as _Path

    from director.init import is_inside_git_repo, run_init

    path = run_init(args.repo, user=args.user, local=args.local)
    print(f"Wrote {path.resolve()}")
    if args.user:
        print("Target forced to user-level via --user.")
    elif args.local:
        print("Target forced to repo-level via --local.")
    else:
        inside = is_inside_git_repo(_Path(args.repo))
        if inside:
            print("Auto-detected: target is a git repo, config written locally.")
        else:
            print("Auto-detected: not inside a git repo, config written to user-level location.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="director", description=__doc__)
    p.add_argument("--version", action="version", version=f"director {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan", help="brainstorm → spec → test-gated DAG, with approval gates")
    pp.add_argument(
        "task", nargs="?", default=None, help="the task description (omit with --continue)"
    )
    pp.add_argument("--repo", default=".", help="target repo (default: .)")
    pp.add_argument(
        "--continue",
        action="store_true",
        dest="continue",
        help="resume after approving/editing the current gate artifact",
    )
    pp.add_argument(
        "--auto", action="store_true", help="planner self-critiques at each gate; no human pause"
    )
    pp.add_argument(
        "--no-critique",
        action="store_true",
        help="with --auto: gates auto-pass without even self-critique",
    )
    pp.set_defaults(func=cmd_plan)

    pr = sub.add_parser("run", help="execute the planned DAG")
    pr.add_argument("--repo", default=".")
    pr.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="max concurrent nodes (default 1; local single-model endpoints want 1)",
    )
    pr.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="executor attempts before escalation (default: config)",
    )
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("status", help="show per-node progress + cost")
    ps.add_argument("--repo", default=".")
    ps.set_defaults(func=cmd_status)

    pu = sub.add_parser(
        "ui", help="serve a live read-only web dashboard of the run DAG (reads .director/)"
    )
    pu.add_argument("--repo", default=".")
    pu.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    pu.add_argument(
        "--port",
        type=int,
        default=8642,
        help="port to serve on (default 8642; 0 picks a free port)",
    )
    pu.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    pu.set_defaults(func=cmd_ui)

    pb = sub.add_parser("bench", help="run one task across profiles; diff cost/quality/wall-time")
    pb.add_argument("task", help="the task description (planned once, run per profile)")
    pb.add_argument("--repo", default=".")
    pb.add_argument(
        "--profiles",
        required=True,
        help="comma-separated profile names, e.g. all-frontier,cheap-cloud,local-first",
    )
    pb.add_argument(
        "--plan-profile",
        default=None,
        help="profile used for the single shared planning pass "
        "(default: all-frontier if listed, else the first profile)",
    )
    pb.add_argument("--parallel", type=int, default=1, help="max concurrent nodes per run")
    pb.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="executor attempts before escalation (default: per-profile config)",
    )
    pb.set_defaults(func=cmd_bench)

    psa = sub.add_parser(
        "sync-agents",
        help="install role agents (writes <repo>/.opencode/ only when an OpenCode provider is configured)",
    )
    psa.add_argument("--repo", default=".")
    psa.set_defaults(func=cmd_sync_agents)

    pi = sub.add_parser("init", help="interactively configure .director/config.toml")
    pi.add_argument("--repo", default=".")
    mut = pi.add_mutually_exclusive_group()
    mut.add_argument("--user", action="store_true")
    mut.add_argument("--local", action="store_true")
    pi.set_defaults(func=cmd_init)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _log(f"director: error: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
