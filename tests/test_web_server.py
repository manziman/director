"""Acceptance tests for the `director ui` backend (director/web/server.py).

The request handling is factored into pure functions, tested here against a
temp `.director/` fixture with no socket:

  * ``build_state(repo)`` merges plan.json + state.json + costs.jsonl +
    metrics.jsonl into the /api/state document: per-node status/glyph/layer,
    the shared ``report.status_summary`` numbers, cost groupings, latest run.
  * Missing plan → ``{"plan_present": False, ...}`` mirroring status_table's
    "No plan found" message.
  * Malformed state.json (a partial write caught mid-poll) → the last-good
    snapshot when provided, else plan-only with every node pending.
  * ``read_node_logs`` returns the node's ``logs/<node>-*.jsonl`` content,
    rejects unknown ids and path-traversal attempts (KeyError) before touching
    disk, and tail-truncates past ``max_bytes``.
  * The ``planning`` section mirrors ``plan_stage.json`` (stage, awaiting gate)
    plus recon.md/spec.md/plan.json presence — populated even before plan.json
    exists, absent when no plan is in progress, last-good on a partial write.
  * ``read_artifact`` serves recon.md / spec.md from a fixed allowlist only.

One thin end-to-end test binds 127.0.0.1:0 in a daemon thread and exercises
``GET /``, ``/api/state``, ``/api/health``, ``/api/logs/<node>`` and the 404s.

Offline, deterministic: temp dirs only, no network beyond loopback.
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.web import server as web  # noqa: E402


def _write_fixture(repo: pathlib.Path, *, state=True, costs=True, metrics=True, logs=True):
    """A 3-node diamond-ish DAG: a ← b, a ← c (b and c concurrent on layer 1)."""
    fdir = repo / ".director"
    (fdir / "logs").mkdir(parents=True, exist_ok=True)
    plan = {
        "job_id": "job-test",
        "task": "add a widget",
        "repo": str(repo),
        "created_at": "2026-01-01T00:00:00",
        "job_branch": "director/job-test",
        "nodes": [
            {
                "id": "a",
                "title": "A",
                "spec": "do a",
                "files": ["a.py"],
                "depends_on": [],
                "test_cmd": "true",
            },
            {
                "id": "b",
                "title": "B",
                "spec": "do b",
                "files": ["b.py"],
                "depends_on": ["a"],
                "test_cmd": "true",
            },
            {
                "id": "c",
                "title": "C",
                "spec": "do c",
                "files": ["c.py"],
                "depends_on": ["a"],
                "test_cmd": "true",
            },
        ],
    }
    (fdir / "plan.json").write_text(json.dumps(plan))
    if state:
        st = {
            "job_id": "job-test",
            "nodes": {
                "a": {
                    "id": "a",
                    "status": "done",
                    "attempts": 1,
                    "tier_used": "executor",
                    "cost_usd": 0.5,
                },
                "b": {"id": "b", "status": "running", "attempts": 1},
                "c": {
                    "id": "c",
                    "status": "escalated",
                    "attempts": 3,
                    "escalated": True,
                    "review_stage_two": True,
                },
            },
        }
        (fdir / "state.json").write_text(json.dumps(st))
    if costs:
        entry = {
            "role": "executor",
            "model": "m1",
            "input": 100,
            "output": 50,
            "cost": 0.5,
            "node": "a",
            "ts": 1.0,
        }
        (fdir / "costs.jsonl").write_text(json.dumps(entry) + "\n")
    if metrics:
        rec = {
            "ts": 2.0,
            "kind": "run",
            "job_id": "job-test",
            "n_nodes": 3,
            "escalation_rate": 33.3,
            "stage_two_trigger_rate": 33.3,
            "executor_tier_pct": 33.3,
            "integration_ok": True,
            "wall_secs": 12.0,
            "cost_total": 0.5,
        }
        (fdir / "metrics.jsonl").write_text(json.dumps(rec) + "\n")
    if logs:
        (fdir / "logs" / "a-executor-1.jsonl").write_text('{"event":"hello-from-a"}\n')


def _write_plan_stage(repo: pathlib.Path, stage: str, **extra):
    """A plan_stage.json as director.plan.PlanProgress persists it."""
    d = {
        "job_id": "job-test",
        "task": "add a widget",
        "job_branch": "director/job-test",
        "stage": stage,
        "auto": False,
        "critique": True,
        **extra,
    }
    fdir = repo / ".director"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "plan_stage.json").write_text(json.dumps(d))


class BuildStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_no_plan(self):
        doc = web.build_state(self.repo)
        self.assertIs(doc["plan_present"], False)
        self.assertIn("No plan found", doc["message"])

    def test_merges_plan_state_cost_metrics(self):
        _write_fixture(self.repo)
        doc = web.build_state(self.repo)
        self.assertIs(doc["plan_present"], True)
        self.assertEqual(doc["job_id"], "job-test")
        self.assertEqual(doc["job_branch"], "director/job-test")
        by_id = {n["id"]: n for n in doc["nodes"]}
        self.assertEqual(len(by_id), 3)
        self.assertEqual(by_id["a"]["state"]["status"], "done")
        self.assertEqual(by_id["a"]["state"]["glyph"], "✅")
        self.assertEqual(by_id["b"]["state"]["status"], "running")
        self.assertEqual(by_id["c"]["state"]["glyph"], "⚠️ ")
        # dependency layers for the layout
        self.assertEqual(by_id["a"]["layer"], 0)
        self.assertEqual(by_id["b"]["layer"], 1)
        self.assertEqual(by_id["c"]["layer"], 1)
        # summary numbers come from report.status_summary
        s = doc["summary"]
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["done"], 1)
        self.assertEqual(s["escalated"], 1)
        self.assertEqual(s["stage_two_reviewed"], 1)
        self.assertEqual(s["by_status"], {"done": 1, "running": 1, "escalated": 1})
        # cost + metrics
        self.assertAlmostEqual(doc["cost"]["total"], 0.5)
        self.assertIn("executor", doc["cost"]["by_role"])
        self.assertEqual(doc["run"]["kind"], "run")
        self.assertEqual(doc["run"]["wall_secs"], 12.0)

    def test_plan_but_no_run_is_all_pending(self):
        _write_fixture(self.repo, state=False, costs=False, metrics=False, logs=False)
        doc = web.build_state(self.repo)
        self.assertTrue(all(n["state"]["status"] == "pending" for n in doc["nodes"]))
        self.assertEqual(doc["summary"]["done"], 0)
        self.assertIsNone(doc["run"])
        self.assertEqual(doc["cost"]["total"], 0.0)

    def test_malformed_state_serves_last_good(self):
        _write_fixture(self.repo)
        good = web.build_state(self.repo)
        (self.repo / ".director" / "state.json").write_text('{"job_id": "job-te')  # truncated
        doc = web.build_state(self.repo, last_good=good)
        self.assertIs(doc, good)

    def test_malformed_state_without_last_good_falls_back_to_pending(self):
        _write_fixture(self.repo)
        (self.repo / ".director" / "state.json").write_text("{oops")
        doc = web.build_state(self.repo)
        self.assertIs(doc["plan_present"], True)
        self.assertTrue(all(n["state"]["status"] == "pending" for n in doc["nodes"]))

    def test_document_is_json_serializable(self):
        _write_fixture(self.repo)
        json.dumps(web.build_state(self.repo))  # must not raise


class PlanningStageTests(unittest.TestCase):
    """The `planning` section of /api/state — plan_stage.json + artifact presence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_planning_absent_when_no_stage_file(self):
        _write_fixture(self.repo)
        self.assertIsNone(web.build_state(self.repo)["planning"])

    def test_gate_spec_before_plan_exists(self):
        # mid-planning: spec written, paused at GATE 1, no plan.json yet
        _write_plan_stage(self.repo, "gate_spec")
        fdir = self.repo / ".director"
        (fdir / "recon.md").write_text("# recon\nrelevant files…\n")
        (fdir / "spec.md").write_text("# spec\ndo the thing\n")
        doc = web.build_state(self.repo)
        self.assertIs(doc["plan_present"], False)
        p = doc["planning"]
        self.assertEqual(p["stage"], "gate_spec")
        self.assertEqual(p["awaiting_gate"], "spec")
        self.assertEqual(p["task"], "add a widget")
        self.assertEqual(p["stages"], ["spec", "gate_spec", "decompose", "gate_plan", "ready"])
        self.assertIs(p["artifacts"]["recon"]["exists"], True)
        self.assertIs(p["artifacts"]["spec"]["exists"], True)
        self.assertIs(p["artifacts"]["plan"]["exists"], False)

    def test_ready_alongside_plan(self):
        _write_fixture(self.repo)
        _write_plan_stage(self.repo, "ready")
        doc = web.build_state(self.repo)
        self.assertIs(doc["plan_present"], True)
        self.assertEqual(doc["planning"]["stage"], "ready")
        self.assertIsNone(doc["planning"]["awaiting_gate"])
        self.assertIs(doc["planning"]["artifacts"]["plan"]["exists"], True)

    def test_gate_plan_awaits_plan_review(self):
        _write_fixture(self.repo)
        _write_plan_stage(self.repo, "gate_plan")
        self.assertEqual(web.build_state(self.repo)["planning"]["awaiting_gate"], "plan")

    def test_malformed_stage_file_without_last_good(self):
        _write_fixture(self.repo)
        (self.repo / ".director" / "plan_stage.json").write_text('{"stage": "gate_')
        doc = web.build_state(self.repo)  # must not raise
        self.assertIsNone(doc["planning"])

    def test_malformed_stage_file_serves_last_good_planning(self):
        _write_fixture(self.repo)
        _write_plan_stage(self.repo, "decompose")
        good = web.build_state(self.repo)
        (self.repo / ".director" / "plan_stage.json").write_text("{oops")
        doc = web.build_state(self.repo, last_good=good)
        self.assertEqual(doc["planning"]["stage"], "decompose")

    def test_planning_document_is_json_serializable(self):
        _write_plan_stage(self.repo, "spec")
        json.dumps(web.build_state(self.repo))  # must not raise


class ReadArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        fdir = self.repo / ".director"
        fdir.mkdir(parents=True)
        (fdir / "spec.md").write_text("# the spec\n")
        (fdir / "recon.md").write_text("# the recon\n")

    def test_returns_artifact_content(self):
        self.assertIn("the spec", web.read_artifact(self.repo, "spec"))
        self.assertIn("the recon", web.read_artifact(self.repo, "recon"))

    def test_unknown_artifact_rejected(self):
        for bad in ("plan", "state", "../spec", "spec.md", ""):
            with self.assertRaises(KeyError, msg=bad):
                web.read_artifact(self.repo, bad)

    def test_missing_artifact_raises_file_not_found(self):
        (self.repo / ".director" / "recon.md").unlink()
        with self.assertRaises(FileNotFoundError):
            web.read_artifact(self.repo, "recon")

    def test_truncates_to_max_bytes(self):
        (self.repo / ".director" / "spec.md").write_text("x" * 10_000)
        text = web.read_artifact(self.repo, "spec", max_bytes=1000)
        self.assertIn("truncated", text)
        self.assertLess(len(text), 2000)


class ReadNodeLogsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _write_fixture(self.repo)

    def test_returns_log_content_with_filename_header(self):
        text = web.read_node_logs(self.repo, "a")
        self.assertIn("a-executor-1.jsonl", text)
        self.assertIn("hello-from-a", text)

    def test_node_without_logs_yet(self):
        self.assertIn("no logs yet", web.read_node_logs(self.repo, "b"))

    def test_unknown_node_id_rejected(self):
        with self.assertRaises(KeyError):
            web.read_node_logs(self.repo, "nope")

    def test_path_traversal_rejected(self):
        secret = self.repo / ".director" / "secret.jsonl"
        secret.write_text("secret")
        for evil in ("../secret", "..", "a/../../secret", "a\\..\\secret"):
            with self.assertRaises(KeyError, msg=evil):
                web.read_node_logs(self.repo, evil)

    def test_no_plan_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(FileNotFoundError):
            web.read_node_logs(empty, "a")

    def test_truncates_to_max_bytes(self):
        big = self.repo / ".director" / "logs" / "a-executor-2.jsonl"
        big.write_text("x" * 10_000)
        text = web.read_node_logs(self.repo, "a", max_bytes=1000)
        self.assertIn("truncated", text)
        self.assertLess(len(text), 2000)


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = pathlib.Path(cls.tmp.name)
        _write_fixture(cls.repo)
        _write_plan_stage(cls.repo, "ready")
        (cls.repo / ".director" / "spec.md").write_text("# spec for e2e\n")
        cls.server = web.DashboardServer(("127.0.0.1", 0), cls.repo)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_index_served(self):
        with self._get("/") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers["Content-Type"])
            self.assertIn(b"director ui", r.read())

    def test_api_state(self):
        with self._get("/api/state") as r:
            doc = json.loads(r.read())
        self.assertIs(doc["plan_present"], True)
        self.assertEqual(len(doc["nodes"]), 3)
        self.assertEqual(doc["planning"]["stage"], "ready")

    def test_api_artifact(self):
        with self._get("/api/artifact/spec") as r:
            self.assertIn(b"spec for e2e", r.read())

    def test_api_artifact_unknown_404(self):
        self._assert_404("/api/artifact/nope")

    def test_api_health(self):
        with self._get("/api/health") as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})

    def test_api_logs(self):
        with self._get("/api/logs/a") as r:
            self.assertIn(b"hello-from-a", r.read())

    def _assert_404(self, path):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(path)
        self.addCleanup(ctx.exception.close)
        self.assertEqual(ctx.exception.code, 404)

    def test_api_logs_unknown_node_404(self):
        self._assert_404("/api/logs/nope")

    def test_api_logs_traversal_404(self):
        self._assert_404("/api/logs/..%2Fsecret")

    def test_unknown_route_404(self):
        self._assert_404("/etc/passwd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
