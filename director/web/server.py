"""Backend for `director ui` — a localhost, read-only observability server.

Everything served here is assembled from files any runtime already writes under
`<repo>/.director/` (plan.json, state.json, costs.jsonl, metrics.jsonl,
logs/<node>-<tier>-<i>.jsonl), so no new instrumentation exists in `run`.

Request handling is factored into pure functions (`build_state`,
`read_node_logs`) so tests exercise them against a temp `.director/` fixture
without a socket. The HTTP layer is a thin stdlib ThreadingHTTPServer.
"""

from __future__ import annotations

import importlib.resources as ir
import json
import urllib.parse
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from director.cost import CostLedger
from director.dag import DagError, topo_order
from director.metrics import latest_run
from director.models import NodeState, Plan
from director.report import _STATUS_GLYPH, status_summary
from director.state import RunState

DEFAULT_PORT = 8642
# Cap on a /api/logs response (tail-truncated), mirroring how run_summary
# truncates integration detail — log files from many attempts can be huge.
MAX_LOG_BYTES = 512_000

_NO_PLAN_MSG = 'No plan found. Run `director plan "<task>"` first.'

# The planning pipeline persisted in plan_stage.json (see director/plan.py):
# spec/decompose are transient, gate_* are where an invocation pauses for review.
_PLAN_STAGES = ["spec", "gate_spec", "decompose", "gate_plan", "ready"]
# Reviewable planning artifacts behind /api/artifact/<name> — a fixed allowlist,
# never a client-supplied path.
_ARTIFACTS = {"recon": "recon.md", "spec": "spec.md"}


def _layers(plan: Plan) -> dict[str, int]:
    """node id -> dependency layer (longest path from a root), for the DAG layout."""
    depth: dict[str, int] = {}
    by_id = {n.id: n for n in plan.nodes}
    try:
        order = topo_order(plan)
    except DagError:
        return {n.id: 0 for n in plan.nodes}
    for nid in order:
        deps = by_id[nid].depends_on
        depth[nid] = 1 + max((depth[d] for d in deps if d in depth), default=-1)
    return depth


def _planning_info(fdir: Path, last_good: dict | None = None) -> dict | None:
    """Planning-pipeline progress: the stage from plan_stage.json plus which
    artifacts (recon.md / spec.md / plan.json) exist so the stepper can show
    what's reviewable. None when no plan is in progress."""
    stage_path = fdir / "plan_stage.json"
    if not stage_path.exists():
        return None
    try:
        d = json.loads(stage_path.read_text())
    except ValueError:  # partial write caught mid-poll
        return (last_good or {}).get("planning")
    artifacts = {}
    for key, fname in (("recon", "recon.md"), ("spec", "spec.md"), ("plan", "plan.json")):
        try:
            st = (fdir / fname).stat()
            artifacts[key] = {"exists": True, "mtime": st.st_mtime, "size": st.st_size}
        except OSError:
            artifacts[key] = {"exists": False, "mtime": None, "size": 0}
    stage = d.get("stage")
    return {
        "stage": stage,
        "stages": _PLAN_STAGES,
        "task": d.get("task"),
        "job_id": d.get("job_id"),
        "job_branch": d.get("job_branch"),
        "auto": d.get("auto", False),
        # which artifact a paused gate is waiting on (approve via `director plan --continue`)
        "awaiting_gate": {"gate_spec": "spec", "gate_plan": "plan"}.get(stage),
        "artifacts": artifacts,
    }


def build_state(repo: str | Path, last_good: dict | None = None) -> dict:
    """Assemble the merged plan + state + summary document behind /api/state.

    `run` rewrites state.json (and appends to the jsonl ledgers) from another
    process, so a poll can rarely catch a partial file; on a malformed read this
    serves `last_good` (the previous successful snapshot) when available.
    """
    repo = Path(repo).resolve()
    fdir = repo / ".director"
    planning = _planning_info(fdir, last_good)
    plan_path = fdir / "plan.json"
    if not plan_path.exists():
        return {"plan_present": False, "message": _NO_PLAN_MSG, "planning": planning}

    try:
        plan = Plan.from_json(plan_path.read_text())
    except (ValueError, KeyError):
        return last_good or {
            "plan_present": False,
            "message": "plan.json is unreadable",
            "planning": planning,
        }

    try:
        state = RunState.load_or_init(repo, plan)
    except (ValueError, TypeError):
        if last_good is not None:
            return last_good
        # No good snapshot yet: fall back to plan-only (everything pending).
        state = RunState(fdir / "state.json", plan.job_id)
        for n in plan.nodes:
            state.nodes[n.id] = NodeState(id=n.id)

    layers = _layers(plan)
    nodes = []
    for n in plan.nodes:
        s = state[n.id]
        nodes.append(
            {
                "id": n.id,
                "title": n.title,
                "spec": n.spec,
                "files": n.files,
                "depends_on": n.depends_on,
                "test_cmd": n.test_cmd,
                "tests": n.tests,
                "estimated_difficulty": n.estimated_difficulty,
                "layer": layers.get(n.id, 0),
                "state": {**asdict(s), "glyph": _STATUS_GLYPH.get(s.status, "?")},
            }
        )

    try:
        ledger = CostLedger(fdir / "costs.jsonl")
        cost = {"total": ledger.total(), "by_role": ledger.by_role(), "by_model": ledger.by_model()}
    except (ValueError, TypeError):  # partial append caught mid-write
        cost = (last_good or {}).get("cost") or {"total": 0.0, "by_role": {}, "by_model": {}}

    try:
        run = latest_run(fdir / "metrics.jsonl")
    except ValueError:
        run = (last_good or {}).get("run")

    return {
        "plan_present": True,
        "job_id": plan.job_id,
        "task": plan.task,
        "job_branch": plan.job_branch,
        "created_at": plan.created_at,
        "planning": planning,
        "nodes": nodes,
        "summary": status_summary(plan, state),
        "cost": cost,
        "run": run,
    }


def read_node_logs(repo: str | Path, node_id: str, max_bytes: int = MAX_LOG_BYTES) -> str:
    """Concatenated raw logs (`logs/<node>-*.jsonl`) for one plan node.

    `node_id` must be an exact id in the loaded plan — anything else (including
    path-traversal attempts) raises KeyError before disk is touched.
    """
    repo = Path(repo).resolve()
    fdir = repo / ".director"
    plan_path = fdir / "plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(_NO_PLAN_MSG)
    if "/" in node_id or "\\" in node_id or ".." in node_id:
        raise KeyError(f"unknown node id: {node_id!r}")
    plan = Plan.from_json(plan_path.read_text())
    if node_id not in {n.id for n in plan.nodes}:
        raise KeyError(f"unknown node id: {node_id!r}")

    logs_dir = (fdir / "logs").resolve()
    parts = []
    if logs_dir.is_dir():
        for p in sorted(logs_dir.glob(f"{node_id}-*.jsonl")):
            if p.resolve().parent != logs_dir:
                continue
            parts.append(f"=== {p.name} ===\n{p.read_text(errors='replace')}")
    text = "\n".join(parts) or f"(no logs yet for node {node_id})"
    raw = text.encode()
    if len(raw) > max_bytes:
        text = "…(truncated)…\n" + raw[-max_bytes:].decode(errors="replace")
    return text


def read_artifact(repo: str | Path, name: str, max_bytes: int = MAX_LOG_BYTES) -> str:
    """A planning artifact (recon.md / spec.md) for the artifact viewer.

    `name` must be a key in the fixed `_ARTIFACTS` allowlist — the client never
    supplies a path. Head-truncated past `max_bytes` (the start of a spec is
    what a reviewer needs)."""
    fname = _ARTIFACTS.get(name)
    if fname is None:
        raise KeyError(f"unknown artifact: {name!r}")
    path = Path(repo).resolve() / ".director" / fname
    if not path.exists():
        raise FileNotFoundError(f"{fname} not written yet")
    text = path.read_text(errors="replace")
    raw = text.encode()
    if len(raw) > max_bytes:
        text = raw[:max_bytes].decode(errors="replace") + "\n…(truncated)…"
    return text


def _index_html() -> bytes:
    return ir.files("director.web").joinpath("index.html").read_bytes()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], repo: str | Path):
        self.repo = Path(repo).resolve()
        self.last_good: dict | None = None
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode())

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler contract)
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _index_html())
        elif path == "/api/state":
            doc = build_state(self.server.repo, last_good=self.server.last_good)
            if doc.get("plan_present"):
                self.server.last_good = doc
            self._send_json(200, doc)
        elif path == "/api/health":
            self._send_json(200, {"ok": True})
        elif path.startswith("/api/logs/"):
            node_id = path[len("/api/logs/") :]
            try:
                text = read_node_logs(self.server.repo, node_id)
            except (KeyError, FileNotFoundError) as e:
                self._send_json(404, {"error": str(e)})
            else:
                self._send(200, "text/plain; charset=utf-8", text.encode())
        elif path.startswith("/api/artifact/"):
            name = path[len("/api/artifact/") :]
            try:
                text = read_artifact(self.server.repo, name)
            except (KeyError, FileNotFoundError) as e:
                self._send_json(404, {"error": str(e)})
            else:
                self._send(200, "text/plain; charset=utf-8", text.encode())
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, *args) -> None:
        pass  # polling every 1.5 s would flood stderr; the CLI logs the URL once


def serve(
    repo: str | Path = ".",
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
    log=print,
) -> None:
    """Serve the dashboard until Ctrl-C. `port=0` picks a free port."""
    server = DashboardServer((host, port), repo)
    url = f"http://{host or '127.0.0.1'}:{server.server_address[1]}/"
    log(f"director ui: watching {server.repo / '.director'}")
    log(f"director ui: dashboard at {url}  (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        log(
            "director ui: WARNING — non-localhost bind exposes unauthenticated read "
            "access to .director/ (specs, logs, file paths) on your network."
        )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("director ui: shutting down")
    finally:
        server.server_close()
