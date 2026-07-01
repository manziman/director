"""Headless OpenAI Codex CLI driver.

Wraps `codex exec --json` with bundled system-prompt templates and parses the
JSONL event stream into a structured RunResult.  Stdlib only.

Event shapes (codex-cli 0.139, `--json`):
  {"type":"thread.started","thread_id":...}
  {"type":"item.completed","item":{"id":...,"type":"agent_message","text":...}}
  {"type":"item.completed","item":{"type":"command_execution","command":...,"status":...}}
  {"type":"turn.completed","usage":{"input_tokens":...,"cached_input_tokens":...,
                                    "output_tokens":...,"reasoning_output_tokens":...}}
  {"type":"error","message":...}
  {"type":"turn.failed","error":{"message":...}}"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

from director import proc as proc_mod
from director.claudecode import system_prompt_for
from director.provider import _CLEAN_ENV, RunResult, register

# Best-effort list of known Codex model aliases (without the codex/ prefix).
# Not authoritative; the `codex` CLI has no "list models" command.  Availability
# differs by auth: gpt-5-codex needs an API key (ChatGPT-plan accounts reject it).
CODEX_MODELS = ("gpt-5.5", "gpt-5-codex")

# item.completed item types that represent tool work (recorded as tool calls).
_TOOL_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "mcp_tool_call", "web_search", "patch_apply"}
)


# --------------------------------------------------------------------------- #
# run_codex
# --------------------------------------------------------------------------- #


def run_codex(
    *,
    agent: str,
    model: str,
    message: str,
    cwd: str | Path,
    log_path: str | Path,
    timeout: int,
) -> RunResult:
    """Invoke Codex CLI headlessly.  Never raises on CLI / model failure."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    err_path = log_path.with_suffix(log_path.suffix + ".stderr")

    sp = system_prompt_for(agent)

    # Build the effective prompt.  Codex has no --append-system-prompt flag,
    # so we prepend the system prompt as a labeled preamble to the positional
    # argument (mechanism choice: inline preamble).
    effective_message = f"{sp}\n\n---\n\n{message}" if sp is not None else message

    # --json: structured JSONL events (text/tokens/tools are unparseable without it).
    # --dangerously-bypass-approvals-and-sandbox: codex exec sandboxes by default,
    # which would block the agent from editing files; director already isolates
    # each run in a disposable git worktree (mirrors claude's
    # --dangerously-skip-permissions).
    # --skip-git-repo-check: don't refuse non-git cwds (e.g. bench scratch dirs).
    cmd = [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-m",
        model,
        effective_message,
    ]

    timed_out = False
    with open(log_path, "wb") as out, open(err_path, "wb") as err:
        handle = proc_mod.popen_tree(cmd, cwd=str(cwd), stdout=out, stderr=err, env=_CLEAN_ENV)
        try:
            rc = handle.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                proc_mod.kill_tree(handle)
            rc = 124
            timed_out = True

    return _parse_codex(log_path, rc, timed_out)


# --------------------------------------------------------------------------- #
# _collect_tool_entry (Codex-specific — mirrors Claude helpers but uses Codex fields)
# --------------------------------------------------------------------------- #


def _collect_tool_entry(
    name: str,
    status: str,
    record: dict,
    tool_calls: list[tuple[str, str]],
    tool_events: list[dict],
) -> None:
    """Append a (name, status) tuple and an event dict to the given lists."""
    tool_calls.append((name, status))
    blob = json.dumps(record, default=str)[:2000].lower()
    tool_events.append({"name": name.lower(), "status": status, "blob": blob})


# --------------------------------------------------------------------------- #
# _parse_codex
# --------------------------------------------------------------------------- #


def _parse_codex(log_path: Path, rc: int, timed_out: bool) -> RunResult:
    """Shape-tolerant parser for Codex CLI JSON / NDJSON output."""
    text_parts: list[str] = []
    tokens = {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
    }
    n_steps: int = 0
    tool_calls: list[tuple[str, str]] = []
    tool_events: list[dict] = []
    error: str | None = None
    cost_reported: float = 0.0

    raw = log_path.read_text(errors="replace")
    stripped = raw.strip()

    # Try single JSON parse first (object, array).
    records: list[dict] = []
    if stripped:
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                records.append(obj)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        records.append(item)
        except (json.JSONDecodeError, TypeError):
            # Fall through to NDJSON line-by-line.
            pass

    # If no records from single parse, try NDJSON fallback.
    if not records:
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except (json.JSONDecodeError, TypeError):
                continue

    # --- aggregate across all parsed records ---------------------------------
    for rec in records:
        rec_type = rec.get("type", "")

        # -- completed items: agent text + tool work ----------------------------
        if rec_type == "item.completed":
            item = rec.get("item")
            if not isinstance(item, dict):
                continue
            n_steps += 1
            item_type = item.get("type", "")
            if item_type == "agent_message":
                t = item.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif item_type in _TOOL_ITEM_TYPES:
                status = item.get("status", "?")
                _collect_tool_entry(str(item_type), str(status), item, tool_calls, tool_events)

        # -- usage (codex reports no cost; pricing config handles $) -----------
        elif rec_type == "turn.completed":
            usage = rec.get("usage")
            if isinstance(usage, dict):
                tokens["input"] += int(usage.get("input_tokens") or 0)
                tokens["output"] += int(usage.get("output_tokens") or 0)
                cr = usage.get("cached_input_tokens")
                if cr is not None:
                    tokens["cache_read"] += int(cr)
                reasoning_val = usage.get("reasoning_output_tokens")
                if reasoning_val is not None:
                    tokens["reasoning"] += int(reasoning_val)

        # -- error ------------------------------------------------------------
        elif rec_type == "error":
            msg = rec.get("message")
            if msg is not None:
                error = str(msg)
        elif rec_type == "turn.failed":
            err_obj = rec.get("error")
            if isinstance(err_obj, dict) and err_obj.get("message") is not None:
                error = str(err_obj["message"])

    # -- raw-stdout-as-text degradation ---------------------------------------
    text = "".join(text_parts).strip()
    if not records and stripped:
        # No parseable JSON at all; fall back to raw stdout.
        text = stripped

    # -- finalize total tokens ------------------------------------------------
    if tokens["total"] == 0:
        tokens["total"] = tokens["input"] + tokens["output"]

    # -- finalize error -------------------------------------------------------
    if rc != 0 and not text and error is None:
        error = f"non-zero exit code {rc}"

    return RunResult(
        returncode=rc,
        text=text,
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
# CodexProvider — Provider protocol adapter
# --------------------------------------------------------------------------- #


class CodexProvider:
    name = "codex"

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
        return run_codex(
            agent=agent,
            model=stripped_model,
            message=message,
            cwd=cwd,
            log_path=log_path,
            timeout=timeout,
        )

    def system_prompt_for(self, agent: str) -> str | None:
        return system_prompt_for(agent)

    def discover_models(self) -> list[str]:
        try:
            return [f"codex/{m}" for m in CODEX_MODELS]
        except Exception:
            return []


register(CodexProvider())
