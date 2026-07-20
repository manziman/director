"""Service install/uninstall/control (director/agent/service.py).

Rendering is pure and asserted on content; lifecycle operations run through an
injected fake `run` so no host service manager is ever touched.
"""

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.agent import service  # noqa: E402


class FakeRun:
    def __init__(self, returncode=0, stdout="", fail=()):
        """`fail` is a tuple of argv prefixes that return exit 1 (everything
        else succeeds) — lets a test fail exactly one lifecycle step."""
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.fail = tuple(tuple(p) for p in fail)

    def __call__(self, argv):
        self.calls.append(argv)
        import types

        rc = self.returncode
        if any(tuple(argv[: len(p)]) == p for p in self.fail):
            rc = 1
        return types.SimpleNamespace(returncode=rc, stdout=self.stdout, stderr="boom" if rc else "")


class RenderTests(unittest.TestCase):
    def test_systemd_unit_shape(self):
        unit = service.render_systemd_unit(8642)
        self.assertIn("[Unit]", unit)
        self.assertIn("ExecStart=", unit)
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        # absolute executable — services do not get a login PATH. Compare via
        # the source argv: the rendered form may be quoted (e.g. Windows paths).
        executable = service.serve_argv(8642)[0]
        self.assertTrue(Path(executable).is_absolute(), exec_line)
        self.assertIn(service._systemd_quote(executable), exec_line)
        self.assertIn("agent serve --host 127.0.0.1 --port 8642", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)
        # secrets never go into the unit; the process loads agent.env itself
        self.assertNotIn("Environment", unit)

    def test_launchd_plist_shape(self):
        plist = service.render_launchd_plist(9000, Path("/tmp/agent.log"))
        self.assertIn(f"<string>{service.LAUNCHD_LABEL}</string>", plist)
        self.assertIn("<string>9000</string>", plist)
        self.assertIn("<key>RunAtLoad</key>", plist)
        self.assertIn("<key>KeepAlive</key>", plist)
        self.assertIn("<key>SuccessfulExit</key>", plist)
        self.assertIn("agent", plist)
        first_arg = plist.split("<array>\n")[1].splitlines()[0].strip()
        executable = first_arg.removeprefix("<string>").removesuffix("</string>")
        self.assertTrue(Path(executable).is_absolute(), first_arg)


class EscapingTests(unittest.TestCase):
    """Paths with spaces/XML metacharacters must survive service rendering."""

    WEIRD = "/weird path/dir&co/<director>"

    def test_systemd_execstart_quotes_unsafe_arguments(self):
        from unittest import mock

        with mock.patch.object(service, "executable_argv", return_value=[self.WEIRD]):
            unit = service.render_systemd_unit(8642)
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertIn(f'"{self.WEIRD}"', exec_line)
        # plain arguments stay unquoted
        self.assertIn(" agent serve ", exec_line)

    def test_systemd_quote_escapes_backslashes_and_quotes(self):
        self.assertEqual(service._systemd_quote('a "b" c\\d'), '"a \\"b\\" c\\\\d"')
        self.assertEqual(service._systemd_quote("/usr/bin/director"), "/usr/bin/director")

    def test_launchd_plist_escapes_xml_metacharacters(self):
        from unittest import mock
        from xml.sax.saxutils import escape

        log = Path("/logs/a&b/agent.log")  # str() differs per platform — compare escaped str()
        with mock.patch.object(service, "executable_argv", return_value=[self.WEIRD]):
            plist = service.render_launchd_plist(8642, log)
        self.assertIn("<string>/weird path/dir&amp;co/&lt;director&gt;</string>", plist)
        self.assertIn(f"<string>{escape(str(log))}</string>", plist)
        self.assertNotIn(self.WEIRD, plist)  # nothing unescaped slips through
        self.assertNotIn("a&b", plist)


class ServiceFailureTests(unittest.TestCase):
    """Service-manager failures surface as ServiceError, never silent success."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_systemd_enable_failure_raises(self):
        run = FakeRun(fail=[("systemctl", "--user", "enable")])
        with self.assertRaises(service.ServiceError) as ctx:
            service.install(8642, platform="linux", home=self.home, run=run)
        self.assertIn("enable", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_systemd_restart_failure_raises(self):
        run = FakeRun(fail=[("systemctl", "--user", "restart")])
        with self.assertRaises(service.ServiceError):
            service.install(8642, platform="linux", home=self.home, run=run)

    def test_launchd_bootstrap_failure_raises(self):
        run = FakeRun(fail=[("launchctl", "bootstrap")])
        with self.assertRaises(service.ServiceError):
            service.install(
                8642, platform="darwin", home=self.home, agent_home=self.home, uid=501, run=run
            )

    def test_launchd_bootout_failure_is_tolerated(self):
        # bootout fails whenever the agent isn't already loaded — the normal
        # first-install path, not an error
        run = FakeRun(fail=[("launchctl", "bootout")])
        report = service.install(
            8642, platform="darwin", home=self.home, agent_home=self.home, uid=501, run=run
        )
        self.assertTrue(any("bootstrapped" in a for a in report["actions"]))

    def test_uninstall_tolerates_disable_failure_but_checks_reload(self):
        report = service.uninstall(
            platform="linux",
            home=self.home,
            run=FakeRun(fail=[("systemctl", "--user", "disable")]),
        )
        self.assertTrue(any("not installed" in a for a in report["actions"]))
        with self.assertRaises(service.ServiceError):
            service.uninstall(
                platform="linux",
                home=self.home,
                run=FakeRun(fail=[("systemctl", "--user", "daemon-reload")]),
            )


class SystemdLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.run = FakeRun()

    def test_install_writes_unit_and_enables(self):
        report = service.install(8642, platform="linux", home=self.home, run=self.run)
        path = service.systemd_unit_path(self.home)
        self.assertTrue(path.exists())
        self.assertIn(["systemctl", "--user", "daemon-reload"], self.run.calls)
        self.assertIn(["systemctl", "--user", "enable", service.SERVICE_NAME], self.run.calls)
        self.assertIn(["systemctl", "--user", "restart", service.SERVICE_NAME], self.run.calls)
        self.assertEqual(report["platform"], "linux")
        self.assertIn("linger", report["note"])

    def test_install_is_idempotent(self):
        service.install(8642, platform="linux", home=self.home, run=self.run)
        report = service.install(8642, platform="linux", home=self.home, run=self.run)
        self.assertTrue(any("unchanged" in a for a in report["actions"]))

    def test_install_no_start_skips_restart(self):
        service.install(8642, start=False, platform="linux", home=self.home, run=self.run)
        self.assertNotIn(["systemctl", "--user", "restart", service.SERVICE_NAME], self.run.calls)

    def test_uninstall_removes_unit_and_is_idempotent(self):
        service.install(8642, platform="linux", home=self.home, run=self.run)
        service.uninstall(platform="linux", home=self.home, run=self.run)
        self.assertFalse(service.systemd_unit_path(self.home).exists())
        report = service.uninstall(platform="linux", home=self.home, run=self.run)
        self.assertTrue(any("not installed" in a for a in report["actions"]))

    def test_control_maps_to_systemctl(self):
        service.control("restart", platform="linux", home=self.home, run=self.run)
        self.assertEqual(
            self.run.calls[-1], ["systemctl", "--user", "restart", service.SERVICE_NAME]
        )


class LaunchdLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.run = FakeRun()

    def test_install_bootstraps_launch_agent(self):
        report = service.install(
            8642, platform="darwin", home=self.home, agent_home=self.home, uid=501, run=self.run
        )
        path = service.launchd_plist_path(self.home)
        self.assertTrue(path.exists())
        self.assertIn(["launchctl", "bootout", "gui/501", str(path)], self.run.calls)
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(path)], self.run.calls)
        self.assertIn(
            ["launchctl", "kickstart", "-k", f"gui/501/{service.LAUNCHD_LABEL}"], self.run.calls
        )
        self.assertEqual(report["platform"], "darwin")

    def test_uninstall_boots_out_and_removes(self):
        service.install(
            8642, platform="darwin", home=self.home, agent_home=self.home, uid=501, run=self.run
        )
        service.uninstall(platform="darwin", home=self.home, uid=501, run=self.run)
        self.assertFalse(service.launchd_plist_path(self.home).exists())

    def test_stop_uses_sigterm(self):
        service.control("stop", platform="darwin", home=self.home, uid=501, run=self.run)
        self.assertEqual(
            self.run.calls[-1],
            ["launchctl", "kill", "SIGTERM", f"gui/501/{service.LAUNCHD_LABEL}"],
        )


class PlatformTests(unittest.TestCase):
    def test_unsupported_platform_raises_with_serve_hint(self):
        with self.assertRaises(service.ServiceError) as ctx:
            service.install(8642, platform="win32", run=FakeRun())
        self.assertIn("director agent serve", str(ctx.exception))

    def test_service_state_reports_unsupported(self):
        state = service.service_state(platform="win32", run=FakeRun())
        self.assertFalse(state["supported"])

    def test_service_state_linux_not_installed(self):
        state = service.service_state(
            platform="linux", home=Path(tempfile.mkdtemp()), run=FakeRun()
        )
        self.assertEqual(
            state, {"platform": "linux", "supported": True, "installed": False, "state": None}
        )


if __name__ == "__main__":
    unittest.main()
