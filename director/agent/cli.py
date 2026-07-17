"""`director agent …` — the orchestrator-facing CLI.

The CLI is an API surface with a documented contract:

  - Every inspection/mutation command supports `--json`; JSON stdout is exactly
    one document (events/logs stream JSON Lines / plain text). Human progress
    and diagnostics go to stderr. No ANSI or cursor control.
  - Read commands (`list/show/wait/logs/events/status`) read the shared job
    storage directly, so they stay useful while the daemon or a runner is down.
    Mutations (`submit/cancel/retry/prune`) talk to the daemon's authenticated
    loopback API.
  - Stable exit codes (see EXIT_*): a successful query exits 0 even when it
    describes a failed job; only `wait` couples its exit code to the job result.

Exit codes:
  0  success (for `wait`: the job succeeded)
  1  `wait`: the job reached a terminal state other than succeeded
  2  usage/config error (also argparse's own code)
  3  agent daemon unavailable (or auth failure against it)
  4  job not found
  5  request/state conflict (idempotency violation, invalid retry, …)
  6  `wait --timeout` elapsed (the job itself is untouched)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from director.agent import service, storage
from director.agent.client import AgentClient, AgentUnavailableError, ApiError
from director.agent.storage import (
    TERMINAL_STATES,
    JobStore,
    UnknownJobError,
    describe_job,
)

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_TIMEOUT = 6

_API_ERROR_EXITS = {
    "job_not_found": EXIT_NOT_FOUND,
    "request_conflict": EXIT_CONFLICT,
    "state_conflict": EXIT_CONFLICT,
    "unauthorized": EXIT_UNAVAILABLE,
}

WAIT_POLL_SECS = 0.5


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _emit(doc: dict) -> None:
    """The single JSON document a --json command prints on stdout."""
    print(json.dumps(doc, indent=2, sort_keys=True))


def _fail(args, code: str, message: str, exit_code: int) -> int:
    if getattr(args, "json", False):
        _emit(
            {"schema_version": storage.SCHEMA_VERSION, "error": {"code": code, "message": message}}
        )
    _log(f"director agent: error: {message}")
    return exit_code


def _guard(fn):
    """Map transport/registry errors onto the documented exit codes."""

    def inner(args) -> int:
        try:
            return fn(args)
        except UnknownJobError as e:
            return _fail(args, "job_not_found", f"unknown job: {e.args[0]!r}", EXIT_NOT_FOUND)
        except AgentUnavailableError as e:
            return _fail(args, "agent_unavailable", str(e), EXIT_UNAVAILABLE)
        except ApiError as e:
            return _fail(args, e.code, str(e), _API_ERROR_EXITS.get(e.code, EXIT_USAGE))

    inner.__name__ = fn.__name__
    return inner


def _store(args) -> JobStore:
    return JobStore(Path(args.data_dir) if args.data_dir else None)


def _client(args) -> AgentClient:
    return AgentClient(Path(args.data_dir) if args.data_dir else None)


def _parse_labels(pairs: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--label expects KEY=VALUE, got {pair!r}")
        labels[key] = value
    return labels


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_serve(args) -> int:
    from director.agent.server import serve

    try:
        serve(
            root=Path(args.data_dir) if args.data_dir else None,
            host=args.host,
            port=args.port,
            max_jobs=args.max_jobs,
            log=_log,
        )
    except OSError as e:
        _log(f"director agent: cannot bind {args.host}:{args.port}: {e} (try --port)")
        return EXIT_USAGE
    return EXIT_OK


@_guard
def cmd_submit(args) -> int:
    if args.input == "-":
        task = sys.stdin.read()
    elif args.input:
        task = Path(args.input).read_text()
    else:
        task = args.task or ""
    payload = {
        "repo": str(Path(args.repo).expanduser().resolve()),
        "task": task,
        "base_ref": args.base,
        "parallel": args.parallel,
        "max_attempts": args.max_attempts,
        "max_cost": args.max_cost,
        "no_critique": args.no_critique,
        "request_id": args.request_id,
        "labels": _parse_labels(args.label),
    }
    doc = _client(args).submit(payload)
    if args.json:
        _emit(doc)
    else:
        print(f"submitted {doc['job_id']} state={doc['state']} branch={doc['job_branch']}")
    return EXIT_OK


@_guard
def cmd_list(args) -> int:
    store = _store(args)
    jobs = []
    for job_id in store.job_ids():
        try:
            doc = describe_job(store, job_id)
        except UnknownJobError:
            continue
        jobs.append(doc)
    if args.state:
        jobs = [j for j in jobs if j["state"] == args.state]
    if args.repo:
        want = str(Path(args.repo).expanduser().resolve())
        jobs = [j for j in jobs if j.get("repo") == want]
    for pair in args.label or []:
        key, _, value = pair.partition("=")
        jobs = [j for j in jobs if (j.get("labels") or {}).get(key) == value]
    jobs.sort(key=lambda j: (j.get("submitted_at") or "", j["job_id"]), reverse=True)
    if args.limit:
        jobs = jobs[: args.limit]
    if args.json:
        _emit({"schema_version": storage.SCHEMA_VERSION, "jobs": jobs})
        return EXIT_OK
    if not jobs:
        print("no jobs")
        return EXIT_OK
    print(f"{'job':32} {'state':12} {'nodes':>8} {'cost':>9}  repo")
    for j in jobs:
        dag = j.get("dag") or {}
        done = (dag.get("by_status") or {}).get("done", 0)
        print(
            f"{j['job_id']:32} {j['state']:12} {done:>3}/{dag.get('total', 0):<4} "
            f"${j.get('cost_total', 0.0):>8.4f}  {j.get('repo', '')}"
        )
    return EXIT_OK


@_guard
def cmd_show(args) -> int:
    doc = describe_job(_store(args), args.job_id)
    if args.json:
        _emit(doc)
        return EXIT_OK
    dag = doc.get("dag") or {}
    lines = [
        f"job:       {doc['job_id']}  ({doc['state']})",
        f"repo:      {doc.get('repo')}",
        f"branch:    {doc.get('job_branch')}  (base {str(doc.get('base_commit'))[:12]})",
        f"workspace: {doc.get('workspace')}",
        f"nodes:     {dag.get('by_status') or {}} of {dag.get('total', 0)}",
        f"cost:      ${doc.get('cost_total', 0.0):.4f}",
        f"runner:    alive={doc.get('runner_alive')} pid={doc.get('pid')}",
        f"message:   {doc.get('message')}  [{doc.get('recommended_action')}]",
    ]
    print("\n".join(lines))
    return EXIT_OK


@_guard
def cmd_wait(args) -> int:
    store = _store(args)
    deadline = time.monotonic() + args.timeout if args.timeout else None
    while True:
        doc = describe_job(store, args.job_id)  # raises UnknownJobError → exit 4
        if doc["state"] in TERMINAL_STATES:
            if args.json:
                _emit(doc)
            else:
                print(f"{doc['job_id']} {doc['state']}")
            return EXIT_OK if doc["state"] == storage.SUCCEEDED else EXIT_JOB_FAILED
        if deadline is not None and time.monotonic() >= deadline:
            return _fail(
                args,
                "wait_timeout",
                f"job {args.job_id} still {doc['state']} after {args.timeout:.0f}s",
                EXIT_TIMEOUT,
            )
        time.sleep(WAIT_POLL_SECS)


@_guard
def cmd_logs(args) -> int:
    store = _store(args)
    store.load_job(args.job_id)
    path = store.job_dir(args.job_id) / "runner.log"

    def read_from(offset: int) -> tuple[str, int]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
                return chunk, f.tell()
        except OSError:
            return "", offset

    if not args.follow:
        text = path.read_text(errors="replace") if path.exists() else ""
        if args.tail is not None:
            lines = text.splitlines()[-args.tail :]
            text = "\n".join(lines) + ("\n" if lines else "")
        sys.stdout.write(text)
        return EXIT_OK

    offset = 0
    if args.tail is not None and path.exists():
        head = path.read_text(errors="replace")
        lines = head.splitlines()[-args.tail :]
        sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
        offset = len(head.encode())
    try:
        while True:
            chunk, offset = read_from(offset)
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            state = store.load_job(args.job_id)["state"]
            if state in TERMINAL_STATES and not chunk:
                return EXIT_OK
            time.sleep(0.5)
    except KeyboardInterrupt:
        return EXIT_OK


@_guard
def cmd_events(args) -> int:
    events = _store(args).read_events(args.job_id, after=args.after)
    for e in events:
        print(json.dumps(e, sort_keys=True))
    return EXIT_OK


@_guard
def cmd_cancel(args) -> int:
    doc = _client(args).cancel(args.job_id)
    if args.json:
        _emit(doc)
    else:
        print(f"{doc['job_id']} {doc['state']}")
    return EXIT_OK


@_guard
def cmd_retry(args) -> int:
    doc = _client(args).retry(args.job_id)
    if args.json:
        _emit(doc)
    else:
        print(f"{doc['job_id']} {doc['state']}")
    return EXIT_OK


@_guard
def cmd_prune(args) -> int:
    doc = _client(args).prune(args.job or None)
    if args.json:
        _emit(doc)
    else:
        pruned = doc.get("pruned", [])
        print(f"pruned {len(pruned)} job(s)" + (": " + ", ".join(pruned) if pruned else ""))
    return EXIT_OK


@_guard
def cmd_status(args) -> int:
    store = _store(args)
    svc = service.service_state()
    try:
        doc = _client(args).status()
        doc["running"] = True
    except AgentUnavailableError as e:
        from director.agent.supervisor import registry_summary

        doc = {
            "schema_version": storage.SCHEMA_VERSION,
            "running": False,
            "detail": str(e),
            "data_dir": str(store.root),
            "jobs": registry_summary(store),
            "agent": storage.read_json(store.root / "agent.json"),
        }
    doc["service"] = svc
    if args.json:
        _emit(doc)
        return EXIT_OK
    if doc["running"]:
        jobs = doc.get("jobs") or {}
        print(f"agent running at {doc.get('endpoint')} (pid {doc.get('pid')})")
        print(f"data:  {doc.get('data_dir')}")
        print(
            f"jobs:  {jobs.get('active', 0)} active, {jobs.get('queued', 0)} queued, "
            f"capacity {jobs.get('capacity')}"
        )
        for w in doc.get("warnings") or []:
            print(f"warning: {w}")
    else:
        print("agent is not running")
        print(f"data:  {doc.get('data_dir')}")
    print(f"service: {svc}")
    return EXIT_OK


def cmd_install(args) -> int:
    try:
        report = service.install(args.port, start=not args.no_start)
    except service.ServiceError as e:
        _log(f"director agent: error: {e}")
        return EXIT_USAGE
    for a in report["actions"]:
        print(a)
    if report.get("note"):
        print(f"note: {report['note']}")
    return EXIT_OK


def cmd_uninstall(args) -> int:
    try:
        report = service.uninstall()
    except service.ServiceError as e:
        _log(f"director agent: error: {e}")
        return EXIT_USAGE
    for a in report["actions"]:
        print(a)
    return EXIT_OK


def _cmd_control(action: str):
    def fn(args) -> int:
        try:
            report = service.control(action)
        except service.ServiceError as e:
            _log(f"director agent: error: {e}")
            return EXIT_USAGE
        print(
            f"{action}: {'ok' if report['ok'] else 'failed'}"
            + (f" ({report['detail']})" if report.get("detail") and not report["ok"] else "")
        )
        return EXIT_OK if report["ok"] else EXIT_USAGE

    fn.__name__ = f"cmd_{action}"
    return fn


def cmd_run_job(args) -> int:
    """Internal: the runner child process entry (spawned by the supervisor)."""
    from director.agent.runner import run_job_process

    return run_job_process(args.job_id, root=Path(args.data_dir) if args.data_dir else None)


# --------------------------------------------------------------------------- #
# parser wiring
# --------------------------------------------------------------------------- #
def add_agent_parser(sub) -> None:
    import argparse

    pa = sub.add_parser(
        "agent",
        help="machine-local job supervisor: submit/monitor many director jobs",
    )
    pa.add_argument(
        "--data-dir",
        default=None,
        help="agent storage root (default: $DIRECTOR_AGENT_HOME or ~/.director/agent)",
    )
    asub = pa.add_subparsers(dest="agent_cmd", required=True)

    def jsonable(p):
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    ps = asub.add_parser("serve", help="run the agent in the foreground")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8642)
    ps.add_argument("--max-jobs", type=int, default=2, help="concurrent job capacity (default 2)")
    ps.set_defaults(func=cmd_serve)

    pb = jsonable(asub.add_parser("submit", help="submit a job (returns after durable acceptance)"))
    pb.add_argument("task", nargs="?", default=None, help="task text (or use --input)")
    pb.add_argument("--repo", required=True, help="target git repository")
    pb.add_argument("--input", metavar="FILE", default=None, help="read task from FILE (- = stdin)")
    pb.add_argument("--base", default=None, help="base ref (default: the repo's current HEAD)")
    pb.add_argument("--parallel", type=int, default=1)
    pb.add_argument("--max-attempts", type=int, default=0)
    pb.add_argument("--max-cost", type=float, default=0.0)
    pb.add_argument("--no-critique", action="store_true")
    pb.add_argument("--request-id", default=None, help="idempotency key for safe retries")
    pb.add_argument(
        "--label", action="append", default=[], metavar="KEY=VALUE", help="repeatable job metadata"
    )
    pb.set_defaults(func=cmd_submit)

    pl = jsonable(asub.add_parser("list", help="list jobs"))
    pl.add_argument("--state", default=None, choices=list(storage.STATES))
    pl.add_argument("--repo", default=None)
    pl.add_argument("--label", action="append", default=[], metavar="KEY=VALUE")
    pl.add_argument("--limit", type=int, default=0, help="newest N jobs")
    pl.set_defaults(func=cmd_list)

    pw = jsonable(asub.add_parser("show", help="full status of one job"))
    pw.add_argument("job_id")
    pw.set_defaults(func=cmd_show)

    pwa = jsonable(asub.add_parser("wait", help="block until the job is terminal"))
    pwa.add_argument("job_id")
    pwa.add_argument(
        "--timeout", type=float, default=0.0, help="give up after SECONDS (exit 6; job untouched)"
    )
    pwa.set_defaults(func=cmd_wait)

    plg = asub.add_parser("logs", help="job runner log")
    plg.add_argument("job_id")
    plg.add_argument("--tail", type=int, default=None, metavar="N")
    plg.add_argument("--follow", action="store_true")
    plg.set_defaults(func=cmd_logs, json=False)

    pe = asub.add_parser("events", help="lifecycle events as JSON Lines")
    pe.add_argument("job_id")
    pe.add_argument(
        "--after", type=int, default=0, metavar="CURSOR", help="events with seq > CURSOR"
    )
    pe.add_argument("--jsonl", "--json", action="store_true", dest="json", help="(always JSONL)")
    pe.set_defaults(func=cmd_events)

    pc = jsonable(asub.add_parser("cancel", help="cancel a job (idempotent)"))
    pc.add_argument("job_id")
    pc.set_defaults(func=cmd_cancel)

    pr = jsonable(asub.add_parser("retry", help="re-queue a failed/cancelled/interrupted job"))
    pr.add_argument("job_id")
    pr.set_defaults(func=cmd_retry)

    pp = jsonable(asub.add_parser("prune", help="delete storage of terminal jobs (branches stay)"))
    pp.add_argument("--job", action="append", default=[], metavar="JOB_ID", help="specific job(s)")
    pp.set_defaults(func=cmd_prune)

    pst = jsonable(asub.add_parser("status", help="agent daemon + service status"))
    pst.set_defaults(func=cmd_status)

    pi = asub.add_parser("install", help="install the user service (systemd/launchd)")
    pi.add_argument("--port", type=int, default=8642)
    pi.add_argument("--no-start", action="store_true")
    pi.set_defaults(func=cmd_install)

    pu = asub.add_parser("uninstall", help="remove the user service")
    pu.set_defaults(func=cmd_uninstall)

    for action in ("start", "stop", "restart"):
        pctl = asub.add_parser(action, help=f"{action} the installed service")
        pctl.set_defaults(func=_cmd_control(action))

    prj = asub.add_parser("run-job", help=argparse.SUPPRESS)
    prj.add_argument("job_id")
    prj.set_defaults(func=cmd_run_job)
