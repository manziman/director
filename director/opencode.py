"""Headless OpenCode dispatch entrypoint.

Resolves a provider via the registry and delegates agent invocation to it.
Re-exports RunResult, _CLEAN_ENV, and provider_for_model from director.provider.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import director.claudecode  # noqa: F401 — ensures ClaudeCodeProvider registers
import director.codex  # noqa: F401 — ensures CodexProvider registers
from director import proc as proc_mod
from director.provider import (
    _CLEAN_ENV,
    RunResult,
    provider_for_model,
    providers,
    register,
)

# --------------------------------------------------------------------------- #
# OpenCodeProvider — Provider protocol adapter for OpenCode CLI
# --------------------------------------------------------------------------- #


class OpenCodeProvider:
    name = "opencode"

    def run(
        self,
        *,
        agent: str,
        model: str,
        message: str,
        cwd: str | Path,
        log_path: str | Path,
        timeout: int,
    ) -> RunResult:
        stripped_model = model.split("/", 1)[1] if "/" in model else model
        return _run_opencode(
            agent=agent,
            model=stripped_model,
            message=message,
            cwd=cwd,
            log_path=log_path,
            timeout=timeout,
        )

    def system_prompt_for(self, agent: str) -> str | None:
        return None

    def discover_models(self) -> list[str]:
        try:
            proc = subprocess.run(
                ["opencode", "models"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            result = []
            for raw in proc.stdout.splitlines():
                stripped = raw.strip()
                if stripped and "/" in stripped:
                    result.append(f"opencode/{stripped}")
            return result
        except Exception:
            return []


register(OpenCodeProvider())


# --------------------------------------------------------------------------- #
# _run_opencode — CLI driver (called by bare name for monkeypatchability)
# --------------------------------------------------------------------------- #


def _run_opencode(
    *,
    agent: str,
    model: str,
    message: str,
    cwd: str | Path,
    log_path: str | Path,
    timeout: int,
) -> RunResult:
    """Run the opencode CLI for a single agent invocation."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    err_path = log_path.with_suffix(log_path.suffix + ".stderr")

    cmd = [
        "opencode",
        "run",
        "--dir",
        str(cwd),
        "--agent",
        agent,
        "--model",
        model,
        "--format",
        "json",
        "--print-logs",
        message,
    ]

    timed_out = False
    with open(log_path, "wb") as out, open(err_path, "wb") as err:
        handle = proc_mod.popen_tree(cmd, cwd=str(cwd), stdout=out, stderr=err, env=_CLEAN_ENV)
        try:
            rc = handle.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc_mod.kill_tree(handle)
            rc = 124
            timed_out = True

    return _parse(log_path, rc, timed_out)


# --------------------------------------------------------------------------- #
# run_agent — public dispatch entrypoint (registry-based)
# --------------------------------------------------------------------------- #


def run_agent(
    *,
    agent: str,
    model: str,
    message: str,
    cwd: str | Path,
    log_path: str | Path,
    timeout: int,
) -> RunResult:
    """Invoke an agent headlessly in `cwd`. The provider is resolved from the
    registry by provider segment (first path component of `model`). Never raises
    on failure — inspect RunResult.ok / .error / .timed_out."""
    prov_entry = provider_for_model(model)
    if prov_entry is None:
        provider = model.split("/", 1)[0]
        known = ", ".join(sorted(p.name for p in providers())) or "(none)"
        return RunResult(
            returncode=2,
            error=f"unknown provider {provider!r} in tier {model!r}: no provider registered (known providers — {known})",
            timed_out=False,
            log_path=str(log_path),
        )
    return prov_entry.run(
        agent=agent,
        model=model,
        message=message,
        cwd=cwd,
        log_path=log_path,
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# _parse — NDJSON event stream parser (unchanged)
# --------------------------------------------------------------------------- #


def _parse(log_path: Path, rc: int, timed_out: bool) -> RunResult:
    text_parts: list[str] = []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "total": 0}
    cost_reported = 0.0
    n_steps = 0
    tool_calls: list[tuple[str, str]] = []
    tool_events: list[dict] = []
    error: str | None = None

    for line in log_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        part = evt.get("part", {}) if isinstance(evt.get("part"), dict) else {}

        if etype == "text":
            text_parts.append(part.get("text", ""))
        elif etype in ("step_finish", "step-finish"):
            n_steps += 1
            tk = part.get("tokens", {}) or {}
            for k in ("input", "output", "reasoning", "total"):
                tokens[k] += int(tk.get(k, 0) or 0)
            cost_reported += float(part.get("cost", 0) or 0)
        elif etype in ("tool", "tool_use"):
            name = part.get("tool", "?")
            status = (part.get("state", {}) or {}).get("status", "?")
            tool_calls.append((name, status))
            blob = json.dumps(part, default=str)[:2000].lower()
            tool_events.append({"name": str(name).lower(), "status": str(status), "blob": blob})
        elif etype in ("error", "invalid_request_error") or "error" in (etype or ""):
            msg = evt.get("error") or part.get("error") or part.get("message") or etype
            error = str(msg)

    if tokens["total"] == 0:
        tokens["total"] = tokens["input"] + tokens["output"]

    return RunResult(
        returncode=rc,
        text="".join(text_parts).strip(),
        tokens=tokens,
        cost_reported=cost_reported,
        n_steps=n_steps,
        tool_calls=tool_calls,
        tool_events=tool_events,
        error=error,
        timed_out=timed_out,
        log_path=str(log_path),
    )


# --------------------------------------------------------------------------- #
# watch-it-fail transcript verification (Phase 3)
# --------------------------------------------------------------------------- #

_EDIT_TOOLS = ("edit", "write", "patch", "create", "apply", "str_replace")
_SHELL_TOOLS = ("bash", "shell", "run", "execute", "terminal", "command")
_TEST_SIGNALS = ("pytest", "unittest", "test", "vitest", "jest", "go test", "cargo test")
_FAIL_SIGNALS = (
    "failed",
    "error",
    "traceback",
    "assertion",
    "exit code 1",
    "non-zero",
    "fail (",
    " failures=",
    "no tests ran",
)


@dataclass
class WatchItFail:
    verdict: str  # "observed" | "not_observed" | "unknown"
    ran_before_edit: bool
    observed_failure: bool
    detail: str

    @property
    def observed(self) -> bool:
        return self.verdict == "observed"


def _is_edit(name: str) -> bool:
    return any(t in name for t in _EDIT_TOOLS)


def _is_shell(name: str) -> bool:
    return any(t in name for t in _SHELL_TOOLS)


def _looks_like_test(blob: str, test_cmd: str) -> bool:
    if any(s in blob for s in _TEST_SIGNALS):
        return True
    # Stack-neutral generic noise tokens (not language-specific binaries)
    _skip = ("-m", "discover", "&&")
    for tok in test_cmd.lower().replace("/", " ").replace(".", " ").split():
        if len(tok) > 3 and tok not in _skip and tok in blob:
            return True
    return False


def watch_it_fail(events: list[dict], test_cmd: str) -> WatchItFail:
    """Did the executor run the (failing) tests BEFORE its first edit? (Phase 3 §1)

    Advisory — the deterministic node gate already enforces tests-pass and
    contract-intact; this verifies the *discipline* (red before green) from the
    transcript, surfaced as a metric. Returns "unknown" when the transcript
    carries no usable tool activity (can't be held against the node)."""
    if not events:
        return WatchItFail("unknown", False, False, "no tool activity in transcript")

    saw_test = False
    saw_failure = False
    for ev in events:
        name, blob = ev.get("name", ""), ev.get("blob", "")
        if _is_shell(name) and _looks_like_test(blob, test_cmd):
            saw_test = True
            if any(s in blob for s in _FAIL_SIGNALS):
                saw_failure = True
            return WatchItFail(
                "observed",
                True,
                saw_failure,
                "ran tests before first edit" + (" (saw failure)" if saw_failure else ""),
            )
        if _is_edit(name):
            return WatchItFail("not_observed", False, False, "edited before running any tests")
    return WatchItFail(
        "unknown", saw_test, saw_failure, "no test run or edit detected among tool calls"
    )
