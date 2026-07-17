"""Native user-service installation for the Director agent.

The agent process always runs in the foreground (`director agent serve`); the
OS service manager owns daemonization, restart-on-failure, and start-at-login:

  Linux   ~/.config/systemd/user/director-agent.service   (systemd --user)
  macOS   ~/Library/LaunchAgents/io.github.manziman.director-agent.plist

Both are user-level on purpose: jobs must run with the user's repository
access, Git identity, provider credentials, and CLI installations — a root
daemon would have none of those. Secrets never go into the unit/plist; the
serve process loads `<agent-home>/agent.env` itself (strict KEY=VALUE, never
shell-evaluated).

Rendering is pure (unit/plist text from parameters) and every lifecycle
operation shells out through an injectable `run` callable, so tests assert the
exact command sequences without touching the host's service manager.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "director-agent.service"
LAUNCHD_LABEL = "io.github.manziman.director-agent"

LINGER_NOTE = (
    "systemd user services stop at logout unless lingering is enabled; run "
    "`loginctl enable-linger $USER` yourself if the agent should keep running "
    "after you log out (director does not perform privileged configuration)."
)


class ServiceError(RuntimeError):
    """Service management is not possible here; the message says why and what
    still works (`director agent serve`)."""


def _default_run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise ServiceError(
            f"{argv[0]} is not available: {e}. Foreground mode still works: `director agent serve`"
        ) from e


def executable_argv() -> list[str]:
    """Absolute invocation for service files (PATH is not reliable in services)."""
    exe = shutil.which("director")
    if exe:
        return [exe]
    return [sys.executable, "-m", "director"]


def serve_argv(port: int) -> list[str]:
    return [*executable_argv(), "agent", "serve", "--host", "127.0.0.1", "--port", str(port)]


# --------------------------------------------------------------------------- #
# rendering (pure)
# --------------------------------------------------------------------------- #
def render_systemd_unit(port: int) -> str:
    exec_start = " ".join(serve_argv(port))
    return (
        "[Unit]\n"
        "Description=Director agent — local job supervisor and dashboard\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_launchd_plist(port: int, log_path: Path) -> str:
    args = "".join(f"      <string>{a}</string>\n" for a in serve_argv(port))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "  <dict>\n"
        f"    <key>Label</key>\n    <string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        f"    <array>\n{args}    </array>\n"
        "    <key>RunAtLoad</key>\n    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <dict>\n      <key>SuccessfulExit</key>\n      <false/>\n    </dict>\n"
        f"    <key>StandardOutPath</key>\n    <string>{log_path}</string>\n"
        f"    <key>StandardErrorPath</key>\n    <string>{log_path}</string>\n"
        "  </dict>\n"
        "</plist>\n"
    )


# --------------------------------------------------------------------------- #
# paths / platform
# --------------------------------------------------------------------------- #
def systemd_unit_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "systemd" / "user" / SERVICE_NAME


def launchd_plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def detect_platform(platform: str | None = None) -> str:
    p = platform or sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    raise ServiceError(
        f"service installation is not supported on {p!r} (Linux systemd user units "
        "and macOS LaunchAgents only). Foreground mode still works: "
        "`director agent serve`"
    )


def _gui_domain(uid: int | None) -> str:
    import os

    return f"gui/{os.getuid() if uid is None else uid}"


# --------------------------------------------------------------------------- #
# lifecycle (idempotent)
# --------------------------------------------------------------------------- #
def install(
    port: int,
    *,
    start: bool = True,
    platform: str | None = None,
    home: Path | None = None,
    agent_home: Path | None = None,
    uid: int | None = None,
    run=_default_run,
) -> dict:
    plat = detect_platform(platform)
    actions: list[str] = []
    if plat == "linux":
        path = systemd_unit_path(home)
        content = render_systemd_unit(port)
        if path.exists() and path.read_text() == content:
            actions.append(f"unit unchanged at {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            actions.append(f"wrote {path}")
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", SERVICE_NAME])
        actions.append("enabled director-agent.service")
        if start:
            run(["systemctl", "--user", "restart", SERVICE_NAME])
            actions.append("started director-agent.service")
        return {"platform": plat, "path": str(path), "actions": actions, "note": LINGER_NOTE}

    path = launchd_plist_path(home)
    from director.agent.storage import agent_home as default_agent_home

    log_path = Path(agent_home or default_agent_home()) / "agent.log"
    content = render_launchd_plist(port, log_path)
    if path.exists() and path.read_text() == content:
        actions.append(f"plist unchanged at {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        actions.append(f"wrote {path}")
    domain = _gui_domain(uid)
    run(["launchctl", "bootout", domain, str(path)])  # idempotent re-load
    run(["launchctl", "bootstrap", domain, str(path)])
    actions.append(f"bootstrapped {LAUNCHD_LABEL}")
    if start:
        run(["launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}"])
        actions.append(f"started {LAUNCHD_LABEL}")
    return {"platform": plat, "path": str(path), "actions": actions, "note": None}


def uninstall(
    *,
    platform: str | None = None,
    home: Path | None = None,
    uid: int | None = None,
    run=_default_run,
) -> dict:
    plat = detect_platform(platform)
    actions: list[str] = []
    if plat == "linux":
        path = systemd_unit_path(home)
        run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        if path.exists():
            path.unlink()
            actions.append(f"removed {path}")
        else:
            actions.append("unit was not installed")
        run(["systemctl", "--user", "daemon-reload"])
        return {"platform": plat, "path": str(path), "actions": actions, "note": None}

    path = launchd_plist_path(home)
    run(["launchctl", "bootout", _gui_domain(uid), str(path)])
    if path.exists():
        path.unlink()
        actions.append(f"removed {path}")
    else:
        actions.append("LaunchAgent was not installed")
    return {"platform": plat, "path": str(path), "actions": actions, "note": None}


def control(
    action: str,
    *,
    platform: str | None = None,
    home: Path | None = None,
    uid: int | None = None,
    run=_default_run,
) -> dict:
    """start | stop | restart the installed service."""
    assert action in ("start", "stop", "restart"), action
    plat = detect_platform(platform)
    if plat == "linux":
        res = run(["systemctl", "--user", action, SERVICE_NAME])
        ok = res.returncode == 0
        return {"platform": plat, "action": action, "ok": ok, "detail": (res.stderr or "").strip()}
    domain = _gui_domain(uid)
    target = f"{domain}/{LAUNCHD_LABEL}"
    if action == "start":
        res = run(["launchctl", "kickstart", target])
    elif action == "stop":
        res = run(["launchctl", "kill", "SIGTERM", target])
    else:
        res = run(["launchctl", "kickstart", "-k", target])
    ok = res.returncode == 0
    return {"platform": plat, "action": action, "ok": ok, "detail": (res.stderr or "").strip()}


def service_state(
    *,
    platform: str | None = None,
    home: Path | None = None,
    uid: int | None = None,
    run=_default_run,
) -> dict:
    """Best-effort service-manager view for `agent status`."""
    try:
        plat = detect_platform(platform)
    except ServiceError:
        return {"platform": sys.platform, "supported": False, "installed": False}
    if plat == "linux":
        installed = systemd_unit_path(home).exists()
        state = None
        if installed:
            res = run(["systemctl", "--user", "is-active", SERVICE_NAME])
            state = (res.stdout or "").strip() or None
        return {"platform": plat, "supported": True, "installed": installed, "state": state}
    installed = launchd_plist_path(home).exists()
    state = None
    if installed:
        res = run(["launchctl", "print", f"{_gui_domain(uid)}/{LAUNCHD_LABEL}"])
        state = "loaded" if res.returncode == 0 else "not-loaded"
    return {"platform": plat, "supported": True, "installed": installed, "state": state}
