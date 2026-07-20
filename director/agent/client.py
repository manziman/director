"""HTTP client for the agent's loopback API (used by CLI mutations).

Read commands deliberately do NOT go through here — they read the shared job
storage directly so `list/show/wait/logs/events` keep working while the daemon
is down or restarting. Mutations (submit/cancel/retry/prune) need the daemon:
it owns the queue and the runner process groups.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from director.agent import storage


class AgentUnavailableError(RuntimeError):
    """The agent daemon is not reachable (no agent.json, or connection refused)."""


class ApiError(RuntimeError):
    """The agent answered with a structured JSON error."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


class AgentClient:
    def __init__(self, root: Path | None = None, timeout: float = 10.0):
        self.root = Path(root) if root else storage.agent_home()
        self.timeout = timeout

    def _endpoint(self) -> str:
        info = storage.read_json(self.root / "agent.json")
        if not info or not info.get("endpoint") or info.get("stopped_at"):
            raise AgentUnavailableError(
                f"no running agent found for {self.root} — start one with "
                "`director agent start` (installed service) or `director agent serve`"
            )
        return info["endpoint"].rstrip("/")

    def _token(self) -> str:
        try:
            return (self.root / "token").read_text().strip()
        except OSError as e:
            raise AgentUnavailableError(f"cannot read agent token: {e}") from e

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self._endpoint() + path
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if method == "POST":
            req.add_header("Authorization", f"Bearer {self._token()}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode()).get("error") or {}
            except ValueError:
                err = {}
            raise ApiError(e.code, err.get("code", "http_error"), err.get("message", str(e))) from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise AgentUnavailableError(f"agent is not reachable: {e}") from e

    # ---- convenience --------------------------------------------------------
    def status(self) -> dict:
        return self.request("GET", "/api/agent")

    def submit(self, payload: dict) -> dict:
        return self.request("POST", "/api/jobs", payload)

    def cancel(self, job_id: str) -> dict:
        return self.request("POST", f"/api/jobs/{job_id}/cancel")

    def retry(self, job_id: str) -> dict:
        return self.request("POST", f"/api/jobs/{job_id}/retry")

    def prune(self, job_ids: list[str] | None = None) -> dict:
        return self.request("POST", "/api/prune", {"job_ids": job_ids})
