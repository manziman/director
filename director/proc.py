"""Shared subprocess execution helpers for the director CLI.

Stdlib-only module that works on both POSIX and Windows without importing
platform-specific attributes at module top level.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from director.provider import _CLEAN_ENV

# --------------------------------------------------------------------------- #
# ShellOutcome
# --------------------------------------------------------------------------- #


@dataclass
class ShellOutcome:
    returncode: int
    output: str  # merged stdout+stderr, single interleaved stream
    timed_out: bool


# --------------------------------------------------------------------------- #
# _shell_argv
# --------------------------------------------------------------------------- #


def _shell_argv(cmd: str) -> list[str]:
    """Return the explicit [shell, flag, cmd] invocation for this platform."""
    if os.name != "nt":
        return ["/bin/sh", "-c", cmd]
    sh = shutil.which("sh")
    if sh is not None:
        return [sh, "-c", cmd]
    bash = shutil.which("bash")
    if bash is not None:
        return [bash, "-c", cmd]
    return ["cmd", "/c", cmd]


# --------------------------------------------------------------------------- #
# run_shell
# --------------------------------------------------------------------------- #


def run_shell(
    cmd: str,
    cwd: Path,
    timeout: int | None = None,
) -> ShellOutcome:
    # Launch in its own process group/session (via popen_tree) so a timeout can
    # kill the WHOLE tree, not just the direct child. Gate/test commands (pytest,
    # `uv run … -m unittest`, `npm test`, …) spawn their own subprocess trees; a bare
    # subprocess.run(timeout=) would SIGKILL only the shell and orphan its children.
    argv = _shell_argv(cmd)
    handle = popen_tree(
        argv,
        cwd=cwd,
        env=_CLEAN_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, _ = handle.communicate(timeout=timeout)
        return ShellOutcome(handle.returncode, out or "", False)
    except subprocess.TimeoutExpired:
        kill_tree(handle)
        # Drain/close the pipes now that the tree is dead. Output is the timeout
        # message, matching the previous subprocess.run(timeout=) contract.
        with contextlib.suppress(Exception):
            handle.communicate()
        return ShellOutcome(124, f"(command timed out after {timeout}s: {cmd})", True)


# --------------------------------------------------------------------------- #
# _popen_group_kwargs / popen_tree
# --------------------------------------------------------------------------- #


def _popen_group_kwargs() -> dict:
    """Return kwargs that isolate the child process into its own group/session."""
    if os.name != "nt":
        return {"start_new_session": True}
    flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP")  # noqa: B009
    return {"creationflags": flag}


def popen_tree(cmd, **kwargs) -> subprocess.Popen:
    """Launch *cmd* in its own process group and return the Popen object."""
    merged = {"stdin": subprocess.DEVNULL, **kwargs, **_popen_group_kwargs()}
    return subprocess.Popen(cmd, **merged)  # noqa: PLC2801


# --------------------------------------------------------------------------- #
# kill_tree
# --------------------------------------------------------------------------- #


def kill_tree(proc_obj: subprocess.Popen) -> None:
    """Best-effort whole-tree kill; suppresses all expected errors."""
    with contextlib.suppress(ProcessLookupError, OSError, AttributeError):
        if os.name != "nt":
            pgid = os.getpgid(proc_obj.pid)
            os.killpg(pgid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc_obj.pid)],
                capture_output=True,
            )
    with contextlib.suppress(ProcessLookupError, OSError, AttributeError):
        proc_obj.wait()
