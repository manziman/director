"""The agent's single loopback server: JSON API + read-only dashboard.

One ThreadingHTTPServer serves three surfaces:

  /api/...            the orchestrator API (reads open; mutations require the
                      bearer token from `<root>/token`)
  /                   a read-only dashboard listing every job
  /job/<job-id>/...   the existing single-run dashboard, scoped to that job's
                      artifact directory (only registered job ids resolve —
                      clients can never supply a filesystem path)

The browser UI is strictly read-only: no mutating endpoint is reachable
without the token, and the pages themselves only GET.
"""

from __future__ import annotations

import hmac
import importlib.resources as ir
import json
import os
import shutil
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from director import __version__
from director.agent import storage
from director.agent.storage import JobStore, RequestConflictError, UnknownJobError, describe_job
from director.agent.supervisor import StateConflictError, SubmitError, Supervisor

DEFAULT_PORT = 8642
MAX_BODY = 1_000_000
MAX_LOG_BYTES = 512_000


def preflight(root: Path) -> list[str]:
    """Environment diagnostics surfaced in `agent status` and at serve startup."""
    warnings: list[str] = []
    if shutil.which("git") is None:
        warnings.append("git is not on PATH — jobs cannot run")
    if not os.environ.get("PATH"):
        warnings.append("PATH is empty — provider CLIs will not resolve")
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError as e:
        warnings.append(f"shared job storage {root} is not writable: {e}")
    try:
        from director.agent.envfile import load_env_file

        load_env_file(root / "agent.env")
    except ValueError as e:
        warnings.append(f"agent.env is malformed: {e}")
    return warnings


def _tail_text(path: Path, tail: int | None, max_bytes: int = MAX_LOG_BYTES) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    if tail is not None:
        text = "\n".join(text.splitlines()[-tail:])
        if text:
            text += "\n"
    raw = text.encode()
    if len(raw) > max_bytes:
        text = "…(truncated)…\n" + raw[-max_bytes:].decode(errors="replace")
    return text


def _agent_html() -> bytes:
    return ir.files("director.web").joinpath("agent.html").read_bytes()


def _job_html() -> bytes:
    return ir.files("director.web").joinpath("index.html").read_bytes()


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: JobStore, sup: Supervisor, token: str):
        self.store = store
        self.sup = sup
        self.token = token
        self.started_at = time.time()
        self.last_good: dict[str, dict] = {}  # job_id -> last good /api/state doc
        super().__init__(address, _Handler)

    def status_doc(self) -> dict:
        return {
            "schema_version": storage.SCHEMA_VERSION,
            "version": __version__,
            "api_version": storage.SCHEMA_VERSION,
            "endpoint": f"http://{self.server_address[0]}:{self.server_address[1]}",
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
            "uptime_secs": round(time.time() - self.started_at, 1),
            "data_dir": str(self.store.root),
            "jobs": self.sup.counts(),
            "warnings": preflight(self.store.root),
        }


class _Handler(BaseHTTPRequestHandler):
    server: AgentServer

    # ---- plumbing -----------------------------------------------------------
    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode())

    def _error(self, code: int, err_code: str, message: str, **details) -> None:
        self._json(code, {"error": {"code": err_code, "message": message, **details}})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        return hmac.compare_digest(header.encode(), expected.encode())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def _route(self) -> tuple[str, dict]:
        parsed = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        return urllib.parse.unquote(parsed.path), query

    def log_message(self, *args) -> None:
        pass

    # ---- GET ----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler contract)
        path, query = self._route()
        try:
            self._get(path, query)
        except UnknownJobError as e:
            self._error(404, "job_not_found", f"unknown job: {e.args[0]!r}")
        except (KeyError, FileNotFoundError) as e:
            self._error(404, "not_found", str(e))

    def _get(self, path: str, query: dict) -> None:
        store = self.server.store
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _agent_html())
        elif path == "/api/health":
            self._json(200, {"ok": True})
        elif path == "/api/agent":
            self._json(200, self.server.status_doc())
        elif path == "/api/jobs":
            self._json(200, self._list_jobs(query))
        elif path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/") :]
            job_id, _, sub = rest.partition("/")
            if sub == "":
                self._json(200, describe_job(store, job_id))
            elif sub == "events":
                after = int(query.get("after") or 0)
                events = store.read_events(job_id, after=after)
                body = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
                self._send(200, "application/x-ndjson; charset=utf-8", body.encode())
            elif sub == "logs":
                store.load_job(job_id)  # 404 on unknown ids before touching disk
                tail = int(query["tail"]) if query.get("tail") else None
                text = _tail_text(store.job_dir(job_id) / "runner.log", tail)
                self._send(200, "text/plain; charset=utf-8", text.encode())
            else:
                self._error(404, "not_found", f"unknown resource {sub!r}")
        elif path.startswith("/job/"):
            self._job_page(path[len("/job/") :])
        else:
            self._error(404, "not_found", "not found")

    def _list_jobs(self, query: dict) -> dict:
        store = self.server.store
        jobs = []
        for job_id in store.job_ids():
            try:
                doc = describe_job(store, job_id)
            except UnknownJobError:
                continue
            jobs.append(doc)
        if query.get("state"):
            jobs = [j for j in jobs if j["state"] == query["state"]]
        if query.get("repo"):
            want = str(Path(query["repo"]).expanduser().resolve())
            jobs = [j for j in jobs if j.get("repo") == want]
        if query.get("label"):
            k, _, v = query["label"].partition("=")
            jobs = [j for j in jobs if (j.get("labels") or {}).get(k) == v]
        jobs.sort(key=lambda j: (j.get("submitted_at") or "", j["job_id"]), reverse=True)
        if query.get("limit"):
            jobs = jobs[: max(0, int(query["limit"]))]
        return {"schema_version": storage.SCHEMA_VERSION, "jobs": jobs}

    def _job_page(self, rest: str) -> None:
        """/job/<id>[/...] — the per-job dashboard, backed by the job's
        artifact directory. Only registered job ids resolve."""
        from director.web.server import build_state_dir, read_artifact_dir, read_node_logs_dir

        store = self.server.store
        job_id, _, sub = rest.partition("/")
        store.load_job(job_id)  # raises UnknownJobError → 404
        artifacts = store.artifacts_dir(job_id)
        if sub == "" and not rest.endswith("/"):
            body = b""
            self.send_response(301)
            self.send_header("Location", f"/job/{job_id}/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif sub in ("", "index.html"):
            self._send(200, "text/html; charset=utf-8", _job_html())
        elif sub == "api/state":
            doc = build_state_dir(artifacts, last_good=self.server.last_good.get(job_id))
            if doc.get("plan_present"):
                self.server.last_good[job_id] = doc
            self._json(200, doc)
        elif sub == "api/health":
            self._json(200, {"ok": True})
        elif sub.startswith("api/logs/"):
            text = read_node_logs_dir(artifacts, sub[len("api/logs/") :])
            self._send(200, "text/plain; charset=utf-8", text.encode())
        elif sub.startswith("api/artifact/"):
            text = read_artifact_dir(artifacts, sub[len("api/artifact/") :])
            self._send(200, "text/plain; charset=utf-8", text.encode())
        else:
            self._error(404, "not_found", f"unknown resource {sub!r}")

    # ---- POST ---------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler contract)
        path, _ = self._route()
        if not self._authorized():
            self._error(401, "unauthorized", "missing or invalid bearer token")
            return
        try:
            self._post(path)
        except UnknownJobError as e:
            self._error(404, "job_not_found", f"unknown job: {e.args[0]!r}")
        except SubmitError as e:
            self._error(400, e.code, str(e))
        except RequestConflictError as e:
            self._error(409, "request_conflict", str(e))
        except StateConflictError as e:
            self._error(409, "state_conflict", str(e))

    def _post(self, path: str) -> None:
        sup = self.server.sup
        if path == "/api/jobs":
            job, created = sup.submit(self._body())
            self._json(201 if created else 200, {"created": created, **job})
        elif path == "/api/prune":
            body = self._body()
            ids = body.get("job_ids")
            pruned = sup.prune([str(i) for i in ids] if ids is not None else None)
            self._json(200, {"schema_version": storage.SCHEMA_VERSION, "pruned": pruned})
        elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path[len("/api/jobs/") : -len("/cancel")]
            self._json(200, sup.cancel(job_id))
        elif path.startswith("/api/jobs/") and path.endswith("/retry"):
            job_id = path[len("/api/jobs/") : -len("/retry")]
            self._json(200, sup.retry(job_id))
        else:
            self._error(404, "not_found", "not found")


def serve(
    root: Path | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    max_jobs: int = 2,
    log=None,
) -> None:
    """Run the agent in the foreground (the service manager owns daemonization)."""
    import sys

    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    store = JobStore(root)
    store.root.mkdir(parents=True, exist_ok=True)

    # Services don't inherit an interactive shell environment; agent.env is the
    # explicit, strictly-parsed substitute (never shell-evaluated).
    env = load_env_strict(store.root)
    os.environ.update(env)
    token = storage.ensure_token(store.root)
    for w in preflight(store.root):
        log(f"director agent: WARNING — {w}")

    sup = Supervisor(store, max_jobs=max_jobs, log=log)
    server = AgentServer((host, port), store, sup, token)
    info = {
        "schema_version": storage.SCHEMA_VERSION,
        "version": __version__,
        "pid": os.getpid(),
        "host": host,
        "port": server.server_address[1],
        "endpoint": f"http://{host}:{server.server_address[1]}",
        "data_dir": str(store.root),
        "max_jobs": max_jobs,
        "started_at": storage.utc_now(),
        "stopped_at": None,
    }
    storage.atomic_write_json(store.root / "agent.json", info)
    url = f"http://{host}:{server.server_address[1]}/"
    log(f"director agent: dashboard at {url}")
    log(f"director agent: data in {store.root}")

    # Service managers stop us with SIGTERM; unwind exactly like Ctrl-C so the
    # shutdown path is identical. (Installing a handler only works in the main
    # thread — tests drive serve() from worker threads and skip this.)
    import contextlib
    import signal

    def _sigterm(*_):
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, _sigterm)

    sup.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("director agent: shutting down (running jobs continue)")
    finally:
        sup.stop()
        server.server_close()
        info["stopped_at"] = storage.utc_now()
        storage.atomic_write_json(store.root / "agent.json", info)


def load_env_strict(root: Path) -> dict[str, str]:
    from director.agent.envfile import load_env_file

    return load_env_file(Path(root) / "agent.env")
