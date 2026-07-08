"""Acceptance tests for the `director auto` one-shot subcommand (node: cli-cmd-auto).

`director auto` fuses plan + run behind a single command, standing up the live
web dashboard for the duration. These tests pin the contract described in the
spec WITHOUT spawning real servers, threads-that-block, browsers, or planners:

  * subparser registration + `func=cmd_auto` dispatch
  * flag defaults / overrides
  * `task` and `--input` are mutually exclusive (argparse SystemExit)
  * lazy-import invariants (run_plan / run_job / DashboardServer never bound at
    cli module top level)
  * module docstring mentions `auto`
  * DashboardServer is constructed BEFORE run_plan is ever called
  * stdin is read exactly once, at the very top, before the server exists
  * run_plan is ALWAYS invoked with auto=True; critique follows --no-critique
  * `--input FILE` / `--input -` content forwarded verbatim (post-strip)
  * exit code 0 clean vs 1 on failed nodes / failed integration gate
  * `--open` implies `--hold`
  * server.shutdown() + server.server_close() both run on success, exception,
    and KeyboardInterrupt
  * resume-skip when stage==READY + plan.json present + task matches
  * task-mismatch RuntimeError names both (truncated ~80 chars)
  * `--force` deletes the five plan artifacts and re-invokes run_plan
  * cost ceiling aborts when CostLedger.total() > --max-cost; no check at 0

All external boundaries are patched at their home modules (director.plan.run_plan,
director.run.run_job, director.web.server.DashboardServer, director.plan.PlanProgress,
director.cost.CostLedger, director.config.load, webbrowser.open). Deterministic;
no real time/network/randomness.

Run: python3 -m unittest discover -s tests -p test_cli_auto.py -q
"""

import contextlib
import io
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys_path_root = pathlib.Path(__file__).resolve().parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(sys_path_root))

from director import cli  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeServer:
    """Stand-in for DashboardServer; records lifecycle events, never binds a port.

    serve_forever() blocks (like the real one) on an Event that shutdown() sets,
    so the daemon thread the CLI spawns behaves realistically but the process
    can always exit (the thread is a daemon) and no CPU is burned.
    """

    events: list = []
    instances: list = []

    def __init__(self, address, repo):
        host, port = address
        self.address = address
        self.repo = repo
        # A real HTTPServer with port 0 would pick a free port; emulate a bound one.
        self.server_address = (host, port or 8642)
        self.shutdown_calls = 0
        self.close_calls = 0
        self._stop = threading.Event()
        FakeServer.events.append("server_init")
        FakeServer.instances.append(self)

    def serve_forever(self):
        self._stop.wait()

    def shutdown(self):
        self.shutdown_calls += 1
        self._stop.set()
        FakeServer.events.append("shutdown")

    def server_close(self):
        self.close_calls += 1
        FakeServer.events.append("server_close")


class FakeStdin:
    def __init__(self, text):
        self.text = text
        self.reads = 0

    def read(self):
        self.reads += 1
        FakeServer.events.append("stdin")
        return self.text


def _clean_result():
    return {
        "job_id": "job-1",
        "done": ["n1"],
        "escalated": [],
        "failed": [],
        "integration_ok": True,
        "by_role": {},
        "by_model": {},
        "cost_total": 0.0,
    }


def _failed_result():
    r = _clean_result()
    r["failed"] = ["n1"]
    return r


def _integration_fail_result():
    r = _clean_result()
    r["integration_ok"] = False
    return r


def _rp_ready(*a, **k):
    FakeServer.events.append("run_plan")
    return SimpleNamespace(stage="ready", message="planning complete")


def _rj_clean(*a, **k):
    return _clean_result()


# --------------------------------------------------------------------------- #
# Parser-level tests (no cmd_auto execution)
# --------------------------------------------------------------------------- #
class TestAutoSubparser(unittest.TestCase):
    def parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_subparser_registered_and_dispatches_cmd_auto(self):
        args = self.parse(["auto", "build a thing"])
        self.assertIs(args.func, cli.cmd_auto)
        self.assertEqual(args.task, "build a thing")

    def test_flag_defaults(self):
        args = self.parse(["auto", "do it"])
        self.assertEqual(args.repo, ".")
        self.assertEqual(args.parallel, 1)
        self.assertEqual(args.max_attempts, 0)
        self.assertFalse(args.no_critique)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8642)
        self.assertFalse(args.open)
        self.assertFalse(args.hold)
        self.assertEqual(args.max_cost, 0.0)
        self.assertFalse(args.force)
        self.assertIsNone(args.input)

    def test_flag_overrides(self):
        args = self.parse(
            [
                "auto",
                "do it",
                "--repo",
                "/tmp/x",
                "--parallel",
                "4",
                "--max-attempts",
                "7",
                "--no-critique",
                "--host",
                "0.0.0.0",
                "--port",
                "9001",
                "--open",
                "--hold",
                "--max-cost",
                "2.5",
                "--force",
            ]
        )
        self.assertEqual(args.repo, "/tmp/x")
        self.assertEqual(args.parallel, 4)
        self.assertEqual(args.max_attempts, 7)
        self.assertTrue(args.no_critique)
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9001)
        self.assertTrue(args.open)
        self.assertTrue(args.hold)
        self.assertEqual(args.max_cost, 2.5)
        self.assertTrue(args.force)

    def test_port_and_parallel_and_max_cost_are_typed(self):
        args = self.parse(["auto", "t", "--port", "0", "--parallel", "2", "--max-cost", "1"])
        self.assertIsInstance(args.port, int)
        self.assertIsInstance(args.parallel, int)
        self.assertIsInstance(args.max_cost, float)

    def test_task_only_ok(self):
        args = self.parse(["auto", "just a task"])
        self.assertEqual(args.task, "just a task")
        self.assertIsNone(args.input)

    def test_input_only_ok(self):
        args = self.parse(["auto", "--input", "prd.md"])
        self.assertIsNone(args.task)
        self.assertEqual(args.input, "prd.md")

    def test_task_and_input_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.parse(["auto", "some task", "--input", "prd.md"])


# --------------------------------------------------------------------------- #
# Module-level invariants
# --------------------------------------------------------------------------- #
class TestModuleInvariants(unittest.TestCase):
    def test_docstring_mentions_auto(self):
        self.assertIsNotNone(cli.__doc__)
        self.assertIn("auto", cli.__doc__)

    def test_cmd_auto_exists_and_is_callable(self):
        self.assertTrue(callable(cli.cmd_auto))

    def test_heavy_symbols_not_bound_at_module_top_level(self):
        # Lazy imports keep these subsystems out of cli's module namespace.
        self.assertFalse(hasattr(cli, "run_plan"))
        self.assertFalse(hasattr(cli, "run_job"))
        self.assertFalse(hasattr(cli, "DashboardServer"))
        self.assertFalse(hasattr(cli, "PlanProgress"))
        self.assertFalse(hasattr(cli, "CostLedger"))

    def test_run_summary_stays_module_level(self):
        # run_summary is imported at top and must remain so.
        self.assertTrue(hasattr(cli, "run_summary"))


# --------------------------------------------------------------------------- #
# Behavioural tests (cmd_auto with all boundaries mocked)
# --------------------------------------------------------------------------- #
class AutoBase(unittest.TestCase):
    def setUp(self):
        FakeServer.events = []
        FakeServer.instances = []

    def tmp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def install(
        self,
        es,
        *,
        run_plan_side=None,
        run_job_side=None,
        load_return=None,
        ledger_total=0.0,
        stdin=None,
    ):
        """Patch every external boundary; return handles to the key mocks."""
        es.enter_context(mock.patch("director.web.server.DashboardServer", FakeServer))
        rp = es.enter_context(
            mock.patch("director.plan.run_plan", side_effect=run_plan_side or _rp_ready)
        )
        rj = es.enter_context(
            mock.patch("director.run.run_job", side_effect=run_job_side or _rj_clean)
        )
        pp = es.enter_context(mock.patch("director.plan.PlanProgress"))
        pp.load.return_value = load_return
        cfg = es.enter_context(mock.patch("director.config.load"))
        cfg.return_value = SimpleNamespace(max_attempts=3)
        led = es.enter_context(mock.patch("director.cost.CostLedger"))
        led.return_value = SimpleNamespace(total=lambda: ledger_total)
        es.enter_context(mock.patch("webbrowser.open"))
        if stdin is not None:
            es.enter_context(mock.patch.object(cli.sys, "stdin", stdin))
        es.enter_context(contextlib.redirect_stdout(io.StringIO()))
        es.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return SimpleNamespace(run_plan=rp, run_job=rj, load=pp.load, ledger=led)

    # -- ordering / lazy construction ------------------------------------- #
    def test_server_constructed_before_run_plan(self):
        with contextlib.ExitStack() as es:
            m = self.install(es)
            args = self.parse(["auto", "do the work", "--repo", self.tmp()])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        self.assertEqual(len(FakeServer.instances), 1)
        m.run_plan.assert_called_once()
        self.assertIn("server_init", FakeServer.events)
        self.assertIn("run_plan", FakeServer.events)
        self.assertLess(
            FakeServer.events.index("server_init"),
            FakeServer.events.index("run_plan"),
            "DashboardServer must be constructed before run_plan is called",
        )

    def test_stdin_read_once_before_server(self):
        stdin = FakeStdin("  task from stdin  \n")
        with contextlib.ExitStack() as es:
            m = self.install(es, stdin=stdin)
            args = self.parse(["auto", "--input", "-", "--repo", self.tmp()])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        self.assertEqual(stdin.reads, 1, "stdin must be read exactly once")
        self.assertLess(
            FakeServer.events.index("stdin"),
            FakeServer.events.index("server_init"),
            "stdin must be read before the server is constructed",
        )
        self.assertEqual(m.run_plan.call_args.args[0], "task from stdin")

    # -- run_plan always auto=True, critique follows flag ------------------ #
    def test_run_plan_always_auto_true(self):
        with contextlib.ExitStack() as es:
            m = self.install(es)
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            cli.cmd_auto(args)
        self.assertIs(m.run_plan.call_args.kwargs["auto"], True)
        self.assertIs(m.run_plan.call_args.kwargs["critique"], True)

    def test_no_critique_forwarded(self):
        with contextlib.ExitStack() as es:
            m = self.install(es)
            args = self.parse(["auto", "task", "--no-critique", "--repo", self.tmp()])
            cli.cmd_auto(args)
        self.assertIs(m.run_plan.call_args.kwargs["auto"], True)
        self.assertIs(m.run_plan.call_args.kwargs["critique"], False)

    # -- task text resolution --------------------------------------------- #
    def test_input_file_content_forwarded_stripped(self):
        repo = self.tmp()
        prd = pathlib.Path(self.tmp()) / "prd.md"
        prd.write_text("   build the feature   \n")
        with contextlib.ExitStack() as es:
            m = self.install(es)
            args = self.parse(["auto", "--input", str(prd), "--repo", repo])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        self.assertEqual(m.run_plan.call_args.args[0], "build the feature")

    def test_positional_task_forwarded_stripped(self):
        with contextlib.ExitStack() as es:
            m = self.install(es)
            args = self.parse(["auto", "  ship it  ", "--repo", self.tmp()])
            cli.cmd_auto(args)
        self.assertEqual(m.run_plan.call_args.args[0], "ship it")

    def test_missing_input_file_raises_filenotfound(self):
        missing = str(pathlib.Path(self.tmp()) / "nope.md")
        with contextlib.ExitStack() as es:
            self.install(es)
            args = self.parse(["auto", "--input", missing, "--repo", self.tmp()])
            with self.assertRaises(FileNotFoundError):
                cli.cmd_auto(args)
        self.assertEqual(FakeServer.instances, [], "server must not be built on bad input")

    def test_empty_stdin_task_raises_valueerror(self):
        stdin = FakeStdin("   \n\t  ")
        with contextlib.ExitStack() as es:
            self.install(es, stdin=stdin)
            args = self.parse(["auto", "--input", "-", "--repo", self.tmp()])
            with self.assertRaises(ValueError):
                cli.cmd_auto(args)
        self.assertEqual(FakeServer.instances, [])

    def test_empty_positional_task_raises_valueerror(self):
        with contextlib.ExitStack() as es:
            self.install(es)
            args = self.parse(["auto", "    ", "--repo", self.tmp()])
            with self.assertRaises(ValueError):
                cli.cmd_auto(args)

    # -- exit codes -------------------------------------------------------- #
    def test_exit_code_zero_on_clean_run(self):
        with contextlib.ExitStack() as es:
            self.install(es, run_job_side=lambda *a, **k: _clean_result())
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)

    def test_exit_code_one_on_failed_nodes(self):
        with contextlib.ExitStack() as es:
            self.install(es, run_job_side=lambda *a, **k: _failed_result())
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 1)

    def test_exit_code_one_on_integration_gate_failure(self):
        with contextlib.ExitStack() as es:
            self.install(es, run_job_side=lambda *a, **k: _integration_fail_result())
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 1)

    # -- --open implies --hold -------------------------------------------- #
    def test_open_implies_hold(self):
        # Force an early post-planning error so we never enter the blocking hold
        # loop, but can still observe that --open flipped args.hold to True.
        def stuck(*a, **k):
            FakeServer.events.append("run_plan")
            return SimpleNamespace(stage="spec", message="stuck")

        with contextlib.ExitStack() as es:
            self.install(es, run_plan_side=stuck)
            args = self.parse(["auto", "task", "--open", "--repo", self.tmp()])
            self.assertFalse(args.hold)
            with self.assertRaises(RuntimeError):
                cli.cmd_auto(args)
        self.assertTrue(args.hold, "--open must set args.hold=True")

    # -- server teardown always happens ----------------------------------- #
    def test_cleanup_on_success(self):
        with contextlib.ExitStack() as es:
            self.install(es)
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            cli.cmd_auto(args)
        srv = FakeServer.instances[-1]
        self.assertEqual(srv.shutdown_calls, 1)
        self.assertEqual(srv.close_calls, 1)

    def test_cleanup_on_exception(self):
        def boom(*a, **k):
            raise RuntimeError("run blew up")

        with contextlib.ExitStack() as es:
            self.install(es, run_job_side=boom)
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            with self.assertRaises(RuntimeError):
                cli.cmd_auto(args)
        srv = FakeServer.instances[-1]
        self.assertEqual(srv.shutdown_calls, 1)
        self.assertEqual(srv.close_calls, 1)

    def test_hold_blocks_before_server_teardown(self):
        # --hold must keep the dashboard SERVING while it blocks; teardown only
        # after the hold ends (regression: an early version held a dead server).
        held = {}

        def fake_hold():
            held["shutdown_calls_at_hold"] = FakeServer.instances[-1].shutdown_calls
            FakeServer.events.append("hold")

        with contextlib.ExitStack() as es:
            self.install(es)
            es.enter_context(mock.patch.object(cli, "_hold_until_interrupt", fake_hold))
            args = self.parse(["auto", "task", "--hold", "--repo", self.tmp()])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        self.assertEqual(
            held["shutdown_calls_at_hold"], 0, "--hold must run while the server still serves"
        )
        self.assertLess(FakeServer.events.index("hold"), FakeServer.events.index("shutdown"))

    def test_cleanup_on_keyboard_interrupt(self):
        def interrupt(*a, **k):
            raise KeyboardInterrupt

        with contextlib.ExitStack() as es:
            self.install(es, run_job_side=interrupt)
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            with contextlib.suppress(KeyboardInterrupt):
                cli.cmd_auto(args)
        srv = FakeServer.instances[-1]
        self.assertEqual(srv.shutdown_calls, 1)
        self.assertEqual(srv.close_calls, 1)

    def test_planning_not_ready_raises_and_cleans_up(self):
        def stuck(*a, **k):
            FakeServer.events.append("run_plan")
            return SimpleNamespace(stage="gate_plan", message="needs review")

        with contextlib.ExitStack() as es:
            m = self.install(es, run_plan_side=stuck)
            args = self.parse(["auto", "task", "--repo", self.tmp()])
            with self.assertRaises(RuntimeError):
                cli.cmd_auto(args)
        m.run_job.assert_not_called()
        srv = FakeServer.instances[-1]
        self.assertEqual(srv.shutdown_calls, 1)
        self.assertEqual(srv.close_calls, 1)

    # -- resume-skip ------------------------------------------------------- #
    def test_resume_skip_when_ready_and_plan_present(self):
        repo = self.tmp()
        ddir = pathlib.Path(repo) / ".director"
        ddir.mkdir(parents=True)
        (ddir / "plan.json").write_text("{}")
        progress = SimpleNamespace(stage="ready", task="the recorded task")
        with contextlib.ExitStack() as es:
            m = self.install(es, load_return=progress)
            args = self.parse(["auto", "the recorded task", "--repo", repo])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        m.run_plan.assert_not_called()
        m.run_job.assert_called_once()

    def test_resume_skip_with_no_task_argument(self):
        repo = self.tmp()
        ddir = pathlib.Path(repo) / ".director"
        ddir.mkdir(parents=True)
        (ddir / "plan.json").write_text("{}")
        progress = SimpleNamespace(stage="ready", task="prior task")
        with contextlib.ExitStack() as es:
            m = self.install(es, load_return=progress)
            args = self.parse(["auto", "--repo", repo])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        m.run_plan.assert_not_called()
        m.run_job.assert_called_once()

    # -- task mismatch ----------------------------------------------------- #
    def test_task_mismatch_raises_runtimeerror_naming_both_truncated(self):
        recorded = "R" * 100
        given = "G" * 100
        progress = SimpleNamespace(stage="gate_plan", task=recorded)
        with contextlib.ExitStack() as es:
            m = self.install(es, load_return=progress)
            args = self.parse(["auto", given, "--repo", self.tmp()])
            with self.assertRaises(RuntimeError) as ctx:
                cli.cmd_auto(args)
        msg = str(ctx.exception)
        self.assertIn("R" * 80, msg)
        self.assertIn("G" * 80, msg)
        self.assertNotIn("R" * 81, msg, "recorded task must be truncated at ~80 chars")
        m.run_plan.assert_not_called()
        m.run_job.assert_not_called()
        self.assertEqual(FakeServer.instances, [], "mismatch aborts before server build")

    # -- --force ----------------------------------------------------------- #
    def test_force_deletes_five_artifacts_and_replans(self):
        repo = self.tmp()
        ddir = pathlib.Path(repo) / ".director"
        ddir.mkdir(parents=True)
        artifacts = ["plan_stage.json", "plan.json", "spec.md", "recon.md", "state.json"]
        for name in artifacts:
            (ddir / name).write_text("stale")
        progress = SimpleNamespace(stage="ready", task="old task")
        with contextlib.ExitStack() as es:
            m = self.install(es, load_return=progress)
            args = self.parse(["auto", "brand new task", "--force", "--repo", repo])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        for name in artifacts:
            self.assertFalse((ddir / name).exists(), f"--force must delete {name}")
        m.run_plan.assert_called_once()
        self.assertEqual(m.run_plan.call_args.args[0], "brand new task")

    def test_force_without_task_errors_before_deleting_artifacts(self):
        # An invalid --force (no task to replan from) must not destroy state
        # (regression: an early version deleted the artifacts before erroring).
        repo = self.tmp()
        ddir = pathlib.Path(repo) / ".director"
        ddir.mkdir(parents=True)
        artifacts = ["plan_stage.json", "plan.json", "spec.md", "recon.md", "state.json"]
        for name in artifacts:
            (ddir / name).write_text("keep")
        with contextlib.ExitStack() as es:
            m = self.install(es, load_return=SimpleNamespace(stage="ready", task="old task"))
            args = self.parse(["auto", "--force", "--repo", repo])
            with self.assertRaises(ValueError):
                cli.cmd_auto(args)
        for name in artifacts:
            self.assertTrue((ddir / name).exists(), f"{name} must survive an invalid --force")
        m.run_plan.assert_not_called()

    def test_force_on_clean_repo_is_noop_then_plans_and_runs(self):
        repo = self.tmp()  # no .director at all
        with contextlib.ExitStack() as es:
            m = self.install(es, load_return=None)
            args = self.parse(["auto", "fresh task", "--force", "--repo", repo])
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        m.run_plan.assert_called_once()
        m.run_job.assert_called_once()

    def test_force_without_task_raises(self):
        repo = self.tmp()
        with contextlib.ExitStack() as es:
            self.install(es, load_return=None)
            args = self.parse(["auto", "--force", "--repo", repo])
            with self.assertRaises(ValueError):
                cli.cmd_auto(args)

    def test_no_progress_and_no_task_raises(self):
        with contextlib.ExitStack() as es:
            self.install(es, load_return=None)
            args = self.parse(["auto", "--repo", self.tmp()])
            with self.assertRaises(ValueError):
                cli.cmd_auto(args)

    # -- cost ceiling ------------------------------------------------------ #
    def test_cost_ceiling_aborts_when_over_budget(self):
        with contextlib.ExitStack() as es:
            m = self.install(es, ledger_total=5.0)
            args = self.parse(["auto", "task", "--max-cost", "1.0", "--repo", self.tmp()])
            with self.assertRaises(RuntimeError):
                cli.cmd_auto(args)
        m.run_job.assert_not_called()
        srv = FakeServer.instances[-1]
        self.assertEqual(srv.shutdown_calls, 1)
        self.assertEqual(srv.close_calls, 1)

    def test_no_cost_check_when_max_cost_zero(self):
        with contextlib.ExitStack() as es:
            m = self.install(es, ledger_total=9999.0)
            args = self.parse(["auto", "task", "--repo", self.tmp()])  # --max-cost default 0
            rc = cli.cmd_auto(args)
        self.assertEqual(rc, 0)
        m.ledger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
