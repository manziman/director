"""Agent shared job storage (director/agent/storage.py).

Covers the durable contracts issue #38 leans on: unique ids, atomic writes,
monotonic duplicate-free event cursors, heartbeat-based liveness (a stale PID
alone is never proof of life), and the derived `show --json` document.
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.agent import storage  # noqa: E402
from director.agent.storage import JobStore, UnknownJobError, describe_job  # noqa: E402


def _request(store: JobStore, job_id: str = None, **over) -> dict:
    job_id = job_id or storage.new_job_id()
    req = {
        "schema_version": 1,
        "request_id": over.get("request_id"),
        "repo": "/tmp/some/repo",
        "task": "do the thing",
        "base_ref": "HEAD",
        "base_commit": "a" * 40,
        "job_id": job_id,
        "job_branch": storage.job_branch_for(job_id),
        "settings": {"parallel": 1},
        "labels": over.get("labels", {}),
        "submitted_at": storage.utc_now(),
    }
    req.update(over)
    return req


class StoreBasicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp)

    def test_job_id_and_branch_shape(self):
        jid = storage.new_job_id()
        self.assertTrue(jid.startswith("job-"))
        self.assertNotEqual(jid, storage.new_job_id())  # random suffix
        self.assertEqual(storage.job_branch_for(jid), f"director/{jid}")

    def test_job_dir_rejects_path_like_ids(self):
        for bad in ("../x", "job-a/../b", "job-a\\b", "notjob-123"):
            with self.assertRaises(UnknownJobError):
                self.store.job_dir(bad)

    def test_create_load_roundtrip_and_queued_state(self):
        req = _request(self.store)
        job = self.store.create_job(req)
        self.assertEqual(job["state"], storage.QUEUED)
        self.assertEqual(job["schema_version"], storage.SCHEMA_VERSION)
        loaded = self.store.load_job(job["job_id"])
        self.assertEqual(loaded, job)
        # paths are absolute and inside the shared root
        self.assertTrue(Path(loaded["workspace"]).is_absolute())
        self.assertTrue(str(loaded["artifact_dir"]).startswith(str(self.tmp)))

    def test_load_unknown_job_raises(self):
        with self.assertRaises(UnknownJobError):
            self.store.load_job("job-00000000-000000-ffffff")

    def test_set_state_stamps_terminal_and_running(self):
        job = self.store.create_job(_request(self.store))
        jid = job["job_id"]
        self.assertIsNone(job["started_at"])
        j2 = self.store.set_state(jid, storage.RUNNING)
        self.assertIsNotNone(j2["started_at"])
        started = j2["started_at"]
        # a resume must keep the original start stamp
        j3 = self.store.set_state(jid, storage.RUNNING)
        self.assertEqual(j3["started_at"], started)
        j4 = self.store.set_state(jid, storage.SUCCEEDED)
        self.assertIsNotNone(j4["completed_at"])

    def test_read_json_tolerates_missing_and_torn_files(self):
        self.assertIsNone(storage.read_json(self.tmp / "nope.json"))
        p = self.tmp / "torn.json"
        p.write_text('{"half":')
        self.assertIsNone(storage.read_json(p))

    def test_token_created_once_with_user_only_permissions(self):
        t1 = storage.ensure_token(self.tmp)
        t2 = storage.ensure_token(self.tmp)
        self.assertEqual(t1, t2)
        if os.name != "nt":  # Windows has no POSIX mode bits
            mode = (self.tmp / "token").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp)
        self.jid = self.store.create_job(_request(self.store))["job_id"]

    def test_seq_is_monotonic_and_cursor_is_duplicate_free(self):
        self.store.append_event(self.jid, "one", {})
        self.store.append_event(self.jid, "two", {})
        events = self.store.read_events(self.jid, after=0)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(set(seqs)))
        cursor = events[1]["seq"]
        rest = self.store.read_events(self.jid, after=cursor)
        self.assertEqual([e["seq"] for e in rest], seqs[2:])

    def test_concurrent_appends_never_duplicate_seq(self):
        def spam(n):
            for _ in range(n):
                self.store.append_event(self.jid, "spam", {})

        threads = [threading.Thread(target=spam, args=(20,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = [e["seq"] for e in self.store.read_events(self.jid)]
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(seqs, sorted(seqs))

    def test_events_for_unknown_job_raise(self):
        with self.assertRaises(UnknownJobError):
            self.store.read_events("job-00000000-000000-ffffff")


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp)
        self.job = self.store.create_job(_request(self.store))
        self.jid = self.job["job_id"]

    def test_alive_requires_pid_and_fresh_heartbeat(self):
        # our own live pid + fresh heartbeat → alive
        self.store.beat(self.jid, os.getpid())
        job = self.store.update_job(self.jid, pid=os.getpid())
        self.assertTrue(self.store.runner_alive(job))

    def test_stale_pid_alone_is_not_alive(self):
        # live PID but NO heartbeat file: never treated as running
        job = self.store.update_job(self.jid, pid=os.getpid())
        self.assertFalse(self.store.runner_alive(job))

    def test_dead_pid_is_not_alive_even_with_fresh_heartbeat(self):
        self.store.beat(self.jid, 2**22 + 12345)
        job = self.store.update_job(self.jid, pid=2**22 + 12345)
        self.assertFalse(self.store.runner_alive(job))

    def test_stale_heartbeat_is_not_alive(self):
        self.store.beat(self.jid, os.getpid())
        hb = self.store.heartbeat_path(self.jid)
        old = hb.stat().st_mtime - storage.STALE_SECS - 10
        os.utime(hb, (old, old))
        job = self.store.update_job(self.jid, pid=os.getpid())
        self.assertFalse(self.store.runner_alive(job))


class DescribeJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp)
        self.jid = self.store.create_job(_request(self.store, labels={"t": "1"}))["job_id"]

    def _write_artifacts(self):
        art = self.store.artifacts_dir(self.jid)
        (art / "plan_stage.json").write_text(json.dumps({"stage": "ready"}))
        plan = {
            "job_id": "x",
            "task": "t",
            "repo": "r",
            "created_at": "now",
            "job_branch": "b",
            "nodes": [
                {"id": "n1", "title": "n1", "spec": "s", "files": []},
                {"id": "n2", "title": "n2", "spec": "s", "files": []},
            ],
        }
        (art / "plan.json").write_text(json.dumps(plan))
        state = {
            "job_id": "x",
            "nodes": {
                "n1": {"id": "n1", "status": "done"},
                "n2": {"id": "n2", "status": "running"},
            },
        }
        (art / "state.json").write_text(json.dumps(state))
        (art / "costs.jsonl").write_text(
            json.dumps({"role": "planner", "model": "m", "input": 1, "output": 1, "cost": 0.5})
            + "\n"
        )

    def test_document_has_progress_cost_and_recommended_action(self):
        self._write_artifacts()
        doc = describe_job(self.store, self.jid)
        self.assertEqual(doc["dag"]["total"], 2)
        self.assertEqual(doc["dag"]["by_status"], {"done": 1, "running": 1})
        self.assertEqual(doc["dag"]["active"], ["n2"])
        self.assertAlmostEqual(doc["cost_total"], 0.5)
        self.assertEqual(doc["task"], "do the thing")
        self.assertEqual(doc["labels"], {"t": "1"})
        self.assertIn("recommended_action", doc)
        self.assertIn("message", doc)
        self.assertTrue(doc["artifacts"]["plan"])
        self.assertFalse(doc["artifacts"]["recon"])

    def test_running_state_with_dead_runner_recommends_reconcile(self):
        self.store.set_state(self.jid, storage.RUNNING, pid=2**22 + 999)
        doc = describe_job(self.store, self.jid)
        self.assertFalse(doc["runner_alive"])
        self.assertEqual(doc["recommended_action"], "restart_agent")

    def test_succeeded_names_the_branch(self):
        self.store.write_result(self.jid, {"ok": True, "error": None, "run": {}, "exit_code": 0})
        self.store.set_state(self.jid, storage.SUCCEEDED)
        doc = describe_job(self.store, self.jid)
        self.assertEqual(doc["recommended_action"], "inspect_branch")
        self.assertTrue(doc["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
