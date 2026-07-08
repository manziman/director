"""director CLI — plan | run | status | ui | bench | sync-agents | init | auto."""

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


def _hold_until_interrupt() -> None:
    """Block (leaving the dashboard serving) until Ctrl-C. Factored out so the
    orchestration tests can stub the wait."""
    import contextlib
    import threading

    stop = threading.Event()
    with contextlib.suppress(KeyboardInterrupt):
        while True:
            stop.wait(1)


def cmd_auto(args) -> int:
    import threading
    import webbrowser
    from pathlib import Path as _Path

    from director.cost import CostLedger
    from director.plan import READY, PlanProgress, run_plan
    from director.run import run_job
    from director.web.server import DashboardServer

    # Resolve the task text FIRST: `--input -` must drain stdin before anything
    # can spawn a child process (children get stdin=DEVNULL — see proc.py).
    if args.input == "-":
        raw = sys.stdin.read()
    elif args.input:
        raw = _Path(args.input).read_text()
    else:
        raw = args.task
    if raw is not None and not raw.strip():
        raise ValueError("empty task input")
    task = raw.strip() if raw is not None else None

    if args.open:  # a human watching in a browser wants the dashboard to outlive the run
        args.hold = True

    repo = _Path(args.repo).resolve()
    cfg = config.load(str(repo))

    # Resume guard — before the server binds, so an invalid invocation does nothing.
    ddir = repo / ".director"
    progress = PlanProgress.load(repo)
    skip_planning = False
    if args.force:
        if task is None:
            raise ValueError("nothing to replan from: --force needs a task or --input")
        for name in ("plan_stage.json", "plan.json", "spec.md", "recon.md", "state.json"):
            (ddir / name).unlink(missing_ok=True)
    elif progress is not None:
        if task is not None and task != progress.task:
            raise RuntimeError(
                f"task mismatch: recorded '{progress.task[:80]}' but got "
                f"'{task[:80]}'; pass --force to replan"
            )
        if progress.stage == READY and (ddir / "plan.json").exists():
            skip_planning = True
    elif task is None:
        raise ValueError("a task is required")

    # Server first: fail fast on a busy port before any tokens are spent.
    server = DashboardServer((args.host, args.port), repo)
    url = f"http://{args.host}:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _log(f"director auto: dashboard at {url}")
    if args.open:
        webbrowser.open(url)

    rc = 0
    try:
        if not skip_planning:
            result = run_plan(task, repo, cfg, _log, auto=True, critique=not args.no_critique)
            if result.stage != READY:
                raise RuntimeError(
                    f"planning did not reach READY (stage={result.stage}): {result.message}"
                )

        if args.max_cost > 0:
            total = CostLedger(ddir / "costs.jsonl").total()
            if total > args.max_cost:
                raise RuntimeError(
                    f"over budget: spent ${total:.4f} exceeds --max-cost ${args.max_cost:.2f}"
                )

        run_result = run_job(
            repo,
            cfg,
            parallel=args.parallel,
            max_attempts=args.max_attempts or cfg.max_attempts,
            log=_log,
        )
        print(run_summary(run_result))
        failed = bool(run_result["failed"]) or not run_result["integration_ok"]
        rc = 1 if failed else 0

        if args.hold:  # hold INSIDE try/finally: the dashboard stays live until Ctrl-C
            _log("director auto: run finished — dashboard held open (Ctrl-C to stop)")
            _hold_until_interrupt()
    finally:
        server.shutdown()
        server.server_close()

    return rc


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

    pa = sub.add_parser("auto", help="one-shot autonomous plan + run with live dashboard")
    me = pa.add_mutually_exclusive_group()
    me.add_argument("task", nargs="?", default=None, help="the PRD/task text (or use --input)")
    me.add_argument(
        "--input", metavar="FILE", default=None, help="read task from FILE; use - for stdin"
    )
    pa.add_argument("--repo", default=".")
    pa.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="max concurrent nodes (default 1)",
    )
    pa.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="executor attempts before escalation (default: config)",
    )
    pa.add_argument("--no-critique", action="store_true")
    pa.add_argument("--host", default="127.0.0.1")
    pa.add_argument(
        "--port",
        type=int,
        default=8642,
    )
    pa.add_argument("--open", action="store_true")
    pa.add_argument("--hold", action="store_true")
    pa.add_argument(
        "--max-cost",
        type=float,
        default=0.0,
    )
    pa.add_argument("--force", action="store_true")
    pa.set_defaults(func=cmd_auto)

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
