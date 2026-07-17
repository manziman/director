"""The agent's loopback HTTP API + read-only dashboard (director/agent/server.py).

A real AgentServer on an ephemeral port, a real (never-started) Supervisor, and
temp repositories. Verifies the auth boundary, the JSON error envelope, job-id
route confinement, and the job-scoped dashboard endpoints.
"""

import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import gitutil  # noqa: E402
from director.agent.server import AgentServer  # noqa: E402
from director.agent.storage import JobStore  # noqa: E402
from director.agent.supervisor import Supervisor  # noqa: E402

TOKEN = "test-token-123"


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        cls.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main", str(cls.repo)], check=True)
        gitutil.git(["config", "user.email", "t@t"], cls.repo)
        gitutil.git(["config", "user.name", "t"], cls.repo)
        (cls.repo / "README.md").write_text("hello\n")
        gitutil.commit_all("init", cls.repo)
        cls.store = JobStore(cls.home)
        cls.sup = Supervisor(cls.store, max_jobs=1, log=lambda m: None)  # never .start()ed
        cls.server = AgentServer(("127.0.0.1", 0), cls.store, cls.sup, TOKEN)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    # ---- helpers ------------------------------------------------------------
    def _get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def _post(self, path, body=None, token=TOKEN):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(req, timeout=5)

    def _submit(self, task="task one", **over):
        body = {"repo": str(self.repo), "task": task, **over}
        with self._post("/api/jobs", body) as r:
            return json.loads(r.read()), r.status

    # ---- dashboard ----------------------------------------------------------
    def test_index_serves_agent_dashboard(self):
        with self._get("/") as r:
            html = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn("director agent", html)

    def test_agent_status_doc(self):
        with self._get("/api/agent") as r:
            doc = json.loads(r.read())
        self.assertEqual(doc["schema_version"], 1)
        self.assertIn("version", doc)
        self.assertIn("jobs", doc)
        self.assertEqual(doc["data_dir"], str(self.home))
        self.assertIsInstance(doc["warnings"], list)

    # ---- auth boundary ------------------------------------------------------
    def test_mutations_require_bearer_token(self):
        for token in (None, "wrong-token"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/jobs", {"repo": str(self.repo), "task": "x"}, token=token)
            self.assertEqual(ctx.exception.code, 401)
            err = json.loads(ctx.exception.read())["error"]
            self.assertEqual(err["code"], "unauthorized")
            ctx.exception.close()

    def test_reads_do_not_require_token(self):
        with self._get("/api/jobs") as r:
            self.assertEqual(r.status, 200)

    # ---- submission ---------------------------------------------------------
    def test_submit_accepts_and_is_idempotent(self):
        doc, status = self._submit(request_id="srv-rid")
        self.assertEqual(status, 201)
        self.assertTrue(doc["created"])
        self.assertEqual(doc["state"], "queued")
        self.assertTrue(doc["job_id"].startswith("job-"))
        again, status2 = self._submit(request_id="srv-rid")
        self.assertEqual(status2, 200)
        self.assertFalse(again["created"])
        self.assertEqual(again["job_id"], doc["job_id"])

    def test_submit_conflict_and_validation_errors(self):
        self._submit(request_id="srv-conflict")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/jobs",
                {"repo": str(self.repo), "task": "other", "request_id": "srv-conflict"},
            )
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(json.loads(ctx.exception.read())["error"]["code"], "request_conflict")
        ctx.exception.close()

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/jobs", {"repo": "/definitely/missing", "task": "x"})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(json.loads(ctx.exception.read())["error"]["code"], "invalid_repo")
        ctx.exception.close()

    # ---- job reads ----------------------------------------------------------
    def test_show_list_events_logs(self):
        doc, _ = self._submit(task="readable job", labels={"k": "v"})
        jid = doc["job_id"]
        with self._get(f"/api/jobs/{jid}") as r:
            shown = json.loads(r.read())
        self.assertEqual(shown["task"], "readable job")
        self.assertIn("recommended_action", shown)

        with self._get("/api/jobs?state=queued") as r:
            listed = json.loads(r.read())["jobs"]
        self.assertIn(jid, [j["job_id"] for j in listed])
        with self._get("/api/jobs?label=k%3Dv") as r:
            labeled = json.loads(r.read())["jobs"]
        self.assertEqual([j["job_id"] for j in labeled], [jid])

        with self._get(f"/api/jobs/{jid}/events?after=0") as r:
            lines = [json.loads(line) for line in r.read().decode().splitlines()]
        self.assertEqual(lines[0]["event"], "submitted")
        cursor = lines[-1]["seq"]
        with self._get(f"/api/jobs/{jid}/events?after={cursor}") as r:
            self.assertEqual(r.read(), b"")

        (self.store.job_dir(jid) / "runner.log").write_text("l1\nl2\nl3\n")
        with self._get(f"/api/jobs/{jid}/logs?tail=2") as r:
            self.assertEqual(r.read().decode(), "l2\nl3\n")

    def test_unknown_job_is_404_with_stable_code(self):
        for path in (
            "/api/jobs/job-00000000-000000-ffffff",
            "/api/jobs/job-00000000-000000-ffffff/events",
            "/job/job-00000000-000000-ffffff/",
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(path)
            self.assertEqual(ctx.exception.code, 404, path)
            ctx.exception.close()

    # ---- job-scoped dashboard ----------------------------------------------
    def test_job_page_and_scoped_state(self):
        doc, _ = self._submit(task="ui job")
        jid = doc["job_id"]
        with self._get(f"/job/{jid}/") as r:
            html = r.read().decode()
        self.assertIn("director ui", html)
        # bare /job/<id> redirects to the slash form (relative fetches depend on it)
        req = urllib.request.Request(self.base + f"/job/{jid}", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.url.rstrip("/"), self.base + f"/job/{jid}")

        with self._get(f"/job/{jid}/api/state") as r:
            state = json.loads(r.read())
        self.assertFalse(state["plan_present"])  # no artifacts yet

        art = self.store.artifacts_dir(jid)
        (art / "spec.md").write_text("# Spec: ui job\n")
        with self._get(f"/job/{jid}/api/artifact/spec") as r:
            self.assertIn("Spec: ui job", r.read().decode())

    def test_path_traversal_job_ids_do_not_resolve(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/job/..%2F..%2Fetc/")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    # ---- mutations ----------------------------------------------------------
    def test_cancel_retry_prune_roundtrip(self):
        doc, _ = self._submit(task="to cancel")
        jid = doc["job_id"]
        with self._post(f"/api/jobs/{jid}/cancel") as r:
            cancelled = json.loads(r.read())
        self.assertEqual(cancelled["state"], "cancelled")
        with self._post(f"/api/jobs/{jid}/cancel") as r:  # idempotent
            self.assertEqual(json.loads(r.read())["state"], "cancelled")

        with self._post(f"/api/jobs/{jid}/retry") as r:
            self.assertEqual(json.loads(r.read())["state"], "queued")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(f"/api/jobs/{jid}/retry")  # queued again → conflict
        self.assertEqual(ctx.exception.code, 409)
        ctx.exception.close()

        with self._post(f"/api/jobs/{jid}/cancel"):
            pass
        with self._post("/api/prune", {"job_ids": [jid]}) as r:
            pruned = json.loads(r.read())["pruned"]
        self.assertEqual(pruned, [jid])
        self.assertFalse(self.store.job_dir(jid).exists())


if __name__ == "__main__":
    unittest.main()
