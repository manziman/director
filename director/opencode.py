"""Headless OpenCode driver.

Wraps `opencode run --agent <role> --model <provider/model> --format json` and
parses the NDJSON event stream into a structured result (assistant text + token
usage + tool activity). This is the ONLY place that shells out to OpenCode.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Director-spawned processes (and the test commands they run) must not litter the
# worktree with Python bytecode: a stray `__pycache__/*.pyc` would otherwise get
# `git add -A`-ed into a node commit, poison later merges, and inflate the
# changed-file count. Suppressing it at the source keeps every worktree clean.
_CLEAN_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


@dataclass
class RunResult:
    returncode: int
    text: str  # concatenated assistant text (e.g. planner JSON)
    tokens: dict  # summed across steps: {input, output, reasoning, total}
    cost_reported: float  # OpenCode's own cost sum (cross-check; often 0 locally)
    n_steps: int
    tool_calls: list[tuple[str, str]] = field(default_factory=list)  # (tool, status)
    tool_events: list[dict] = field(default_factory=list)  # ordered {name,status,blob}
    error: str | None = None
    timed_out: bool = False
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None and not self.timed_out


def run_agent(
    *,
    agent: str,
    model: str,
    message: str,
    cwd: str | Path,
    log_path: str | Path,
    timeout: int,
) -> RunResult:
    """Invoke an OpenCode agent headlessly in `cwd`. NDJSON events go to
    `log_path`; OpenCode logs go to `log_path + '.stderr'`. Never raises on a
    model/agent failure — inspect RunResult.ok / .error / .timed_out."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    err_path = log_path.with_suffix(log_path.suffix + ".stderr")

    # --dir pins the project/worktree explicitly. Without it, `opencode run`
    # resolves the project root by walking up for a .git and can land on an
    # ENCLOSING repo (so edits leak out of an isolated worktree). Worktrees are
    # also placed outside the repo tree (see run.py) to make this airtight.
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
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=err, env=_CLEAN_ENV)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            rc = 124
            timed_out = True

    return _parse(log_path, rc, timed_out)


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
            # keep an ordered, capped content blob per tool event so the
            # watch-it-fail analyzer can tell a test run from a file edit and spot
            # a failure in the command output (shape-tolerant: just stringify part).
            blob = json.dumps(part, default=str)[:2000].lower()
            tool_events.append({"name": str(name).lower(), "status": str(status), "blob": blob})
        elif etype in ("error", "invalid_request_error") or "error" in (etype or ""):
            # error events may be at top level (evt["error"]) or in part
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
# Tool-name fragments. OpenCode names vary by provider/model, so match on
# substrings rather than exact names.
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
    # also match a distinctive token from the configured test command
    _skip = ("python", "python3", "-m", "discover", "&&")
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
            # an edit happened before any test run → watch-it-fail not honored
            return WatchItFail("not_observed", False, False, "edited before running any tests")
    # tools ran but none matched a test command or an edit
    return WatchItFail(
        "unknown", saw_test, saw_failure, "no test run or edit detected among tool calls"
    )
