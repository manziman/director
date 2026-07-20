"""`director agent …` CLI contract (director/agent/cli.py).

Drives director.cli.main() directly and pins the orchestrator-facing contract:
exactly one JSON document on stdout under --json, diagnostics on stderr, and
the documented exit codes (0 ok / 1 wait-failure / 2 usage / 3 daemon down /
4 not found / 5 conflict / 6 wait timeout). No daemon runs in these tests —
reads must work from storage alone, and mutations must fail with exit 3.
"""

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import cli  # noqa: E402
from director.agent import storage  # noqa: E402
from director.agent.storage import JobStore  # noqa: E402


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self._saved = os.environ.get("DIRECTOR_AGENT_HOME")
        os.environ["DIRECTOR_AGENT_HOME"] = str(self.home)
        self.store = JobStore(self.home)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DIRECTOR_AGENT_HOME", None)
        else:
            os.environ["DIRECTOR_AGENT_HOME"] = self._saved

    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["agent", *argv])
        return rc, out.getvalue(), err.getvalue()

    def _make_job(self, state=storage.QUEUED, **over) -> str:
        jid = storage.new_job_id()
        self.store.create_job(
            {
                "schema_version": 1,
                "request_id": over.get("request_id"),
                "repo": "/tmp/repo",
                "task": "cli test task",
                "base_ref": "HEAD",
                "base_commit": "b" * 40,
                "job_id": jid,
                "job_branch": storage.job_branch_for(jid),
                "settings": {"parallel": 1},
                "labels": over.get("labels", {}),
                "submitted_at": storage.utc_now(),
            }
        )
        if state != storage.QUEUED:
            if state in storage.TERMINAL_STATES:
                ok = state == storage.SUCCEEDED
                self.store.write_result(
                    jid,
                    {
                        "ok": ok,
                        "error": None if ok else {"code": "x", "message": "x"},
                        "run": {},
                        "exit_code": 0 if ok else 1,
                    },
                )
            self.store.set_state(jid, state)
        return jid

    # ---- reads work with no daemon ------------------------------------------
    def test_list_json_is_one_parseable_document(self):
        self._make_job()
        rc, out, _ = self._run("list", "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)  # exactly one document
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(len(doc["jobs"]), 1)

    def test_list_filters_by_state_and_label(self):
        a = self._make_job(labels={"team": "x"})
        self._make_job(state=storage.SUCCEEDED)
        rc, out, _ = self._run("list", "--json", "--state", "queued", "--label", "team=x")
        jobs = json.loads(out)["jobs"]
        self.assertEqual([j["job_id"] for j in jobs], [a])

    def test_show_json_and_human(self):
        jid = self._make_job()
        rc, out, _ = self._run("show", jid, "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["job_id"], jid)
        rc, out, _ = self._run("show", jid)
        self.assertEqual(rc, 0)
        self.assertIn(jid, out)

    def test_show_unknown_job_exits_4(self):
        rc, out, err = self._run("show", "job-00000000-000000-ffffff", "--json")
        self.assertEqual(rc, 4)
        self.assertEqual(json.loads(out)["error"]["code"], "job_not_found")
        self.assertIn("unknown job", err)

    def test_events_jsonl_and_cursor(self):
        jid = self._make_job()
        self.store.append_event(jid, "extra", {"n": 1})
        rc, out, _ = self._run("events", jid)
        self.assertEqual(rc, 0)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertGreaterEqual(len(lines), 2)
        cursor = lines[0]["seq"]
        rc, out2, _ = self._run("events", jid, "--after", str(cursor))
        self.assertEqual(
            [e["seq"] for e in map(json.loads, out2.splitlines())], [e["seq"] for e in lines[1:]]
        )

    def test_logs_tail(self):
        jid = self._make_job()
        (self.store.job_dir(jid) / "runner.log").write_text("a\nb\nc\n")
        rc, out, _ = self._run("logs", jid, "--tail", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "b\nc\n")

    def test_logs_json_is_one_parseable_document(self):
        jid = self._make_job()
        (self.store.job_dir(jid) / "runner.log").write_text("a\nb\nc\n")
        rc, out, _ = self._run("logs", jid, "--tail", "2", "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["job_id"], jid)
        self.assertEqual(doc["log"], "b\nc\n")
        self.assertEqual(doc["tail"], 2)

    def test_status_reports_not_running_and_exits_0(self):
        rc, out, _ = self._run("status", "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertFalse(doc["running"])
        self.assertIn("service", doc)
        self.assertEqual(doc["data_dir"], str(self.home))

    # ---- wait semantics ------------------------------------------------------
    def test_wait_succeeded_exits_0(self):
        jid = self._make_job(state=storage.SUCCEEDED)
        rc, out, _ = self._run("wait", jid, "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["state"], "succeeded")

    def test_wait_failed_exits_1_but_still_emits_the_document(self):
        jid = self._make_job(state=storage.FAILED)
        rc, out, _ = self._run("wait", jid, "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["state"], "failed")

    def test_wait_timeout_exits_6_and_does_not_mutate(self):
        jid = self._make_job()  # queued forever (no daemon)
        rc, out, err = self._run("wait", jid, "--timeout", "0.4", "--json")
        self.assertEqual(rc, 6)
        self.assertEqual(json.loads(out)["error"]["code"], "wait_timeout")
        self.assertEqual(self.store.load_job(jid)["state"], storage.QUEUED)

    def test_wait_unknown_job_exits_4(self):
        rc, _, _ = self._run("wait", "job-00000000-000000-ffffff")
        self.assertEqual(rc, 4)

    # ---- mutations need the daemon ------------------------------------------
    def test_mutations_exit_3_when_daemon_is_down(self):
        jid = self._make_job()
        for argv in (
            ["cancel", jid],
            ["retry", jid],
            ["prune"],
            ["submit", "--repo", "/tmp/repo", "task text"],
        ):
            rc, _, err = self._run(*argv)
            self.assertEqual(rc, 3, argv)
            self.assertIn("agent", err)

    def test_malformed_label_is_a_usage_error(self):
        rc, _, err = self._run("submit", "--repo", "/tmp/repo", "--label", "notakv", "task")
        self.assertEqual(rc, 2)
        self.assertIn("KEY=VALUE", err)

    def test_json_error_document_on_stdout_for_json_mode(self):
        rc, out, _ = self._run("cancel", "job-00000000-000000-ffffff", "--json")
        self.assertEqual(rc, 3)  # daemon down beats not-found: no endpoint to ask
        doc = json.loads(out)
        self.assertEqual(doc["error"]["code"], "agent_unavailable")


if __name__ == "__main__":
    unittest.main()
