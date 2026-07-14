"""Headless Pi coding-agent CLI driver.

Pi emits a JSON-lines event stream in print mode.  This adapter keeps the
provider boundary deliberately small: Director supplies the role prompt and
task message, while Pi remains responsible for model access and tools.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any

from director import proc as proc_mod
from director.claudecode import system_prompt_for as _system_prompt_for
from director.provider import _CLEAN_ENV, RunResult, register

_TEXT_BLOCK_TYPES = frozenset({"text", "output_text", "input_text"})
_TOOL_EVENT_TYPES = frozenset(
    {"tool_execution_start", "tool_execution_update", "tool_execution_end"}
)
_ERROR_EVENT_TYPES = frozenset(
    {"error", "turn_error", "turn_failed", "agent_error", "extension_error"}
)


def run_pi(
    *,
    agent: str,
    model: str,
    message: str,
    cwd: str | Path,
    log_path: str | Path,
    timeout: int,
) -> RunResult:
    """Invoke Pi in non-interactive JSON mode and parse its event stream."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    err_path = log_path.with_suffix(log_path.suffix + ".stderr")
    role_prompt = _system_prompt_for(agent)

    cmd = [
        "pi",
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--model",
        model,
        "--append-system-prompt",
        role_prompt or "",
        message,
    ]
    env = {**_CLEAN_ENV, "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}

    timed_out = False
    with open(log_path, "wb") as out, open(err_path, "wb") as err:
        handle = proc_mod.popen_tree(cmd, cwd=str(cwd), stdout=out, stderr=err, env=env)
        try:
            rc = handle.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                proc_mod.kill_tree(handle)
            rc = 124
            timed_out = True

    return _parse_pi(log_path, rc, timed_out)


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_number(value: Any, names: tuple[str, ...]) -> int | float | None:
    for obj in _walk_dicts(value):
        for name in names:
            number = _numeric(obj.get(name))
            if number is not None:
                return number
    return None


def _usage(value: Any) -> dict[str, int | float]:
    fields = {
        "input": ("input", "inputTokens", "input_tokens", "promptTokens", "prompt_tokens"),
        "output": (
            "output",
            "outputTokens",
            "output_tokens",
            "completionTokens",
            "completion_tokens",
        ),
        "cache_read": (
            "cacheRead",
            "cache_read",
            "cacheReadTokens",
            "cache_read_tokens",
            "cachedInputTokens",
            "cached_input_tokens",
        ),
        "cache_write": (
            "cacheWrite",
            "cache_write",
            "cacheWriteTokens",
            "cache_write_tokens",
        ),
        "total": ("totalTokens", "total_tokens", "total_token_count", "total_token_counts"),
    }
    result: dict[str, int | float] = {}
    for key, names in fields.items():
        number = _first_number(value, names)
        if number is not None:
            result[key] = number
    return result


def _cost(value: Any) -> float:
    for obj in _walk_dicts(value):
        for key in ("costUsd", "cost_usd", "totalCost", "total_cost"):
            number = _numeric(obj.get(key))
            if number is not None:
                return float(number)
        cost = obj.get("cost")
        number = _numeric(cost)
        if number is not None:
            return float(number)
        if isinstance(cost, dict):
            number = _first_number(cost, ("total", "value", "amount"))
            if number is not None:
                return float(number)
    return 0.0


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    if message.get("role") not in (None, "assistant"):
        return ""
    content = message.get("content", message.get("text", ""))

    def flatten(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            result: list[str] = []
            for item in value:
                result.extend(flatten(item))
            return result
        if isinstance(value, dict):
            block_type = value.get("type")
            if block_type in _TEXT_BLOCK_TYPES or block_type is None:
                for key in ("text", "value", "content"):
                    if key in value:
                        return flatten(value[key])
        return []

    return "".join(flatten(content)).strip()


def _diagnostic(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("message", "errorMessage", "finalError", "error"):
            result = _diagnostic(value.get(key))
            if result:
                return result
    return None


def _tool_result_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("error", "errorMessage", "message", "content", "text"):
            result = _tool_result_text(value.get(key))
            if result:
                return result
    return None


def _message_diagnostic(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    diagnostic = _diagnostic(message)
    if diagnostic:
        return diagnostic
    stop_reason = message.get("stopReason")
    if stop_reason in {"error", "failed", "aborted"}:
        return f"assistant message stopped with {stop_reason}"
    return None


def _tool_event(record: dict, name: str, status: str) -> dict:
    event = {
        "name": name.lower(),
        "status": status,
        "blob": json.dumps(record, default=str)[:2000].lower(),
    }
    for key in ("toolCallId", "args", "result", "partialResult", "isError"):
        if key in record:
            event[key] = record[key]
    return event


def _parse_pi(log_path: Path, rc: int, timed_out: bool) -> RunResult:
    """Parse Pi JSONL defensively; malformed or unknown records are ignored."""
    try:
        raw = log_path.read_text(errors="replace")
    except OSError:
        raw = ""

    records: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(record, dict):
            records.append(record)

    n_steps = 0
    agent_text = ""
    message_text = ""
    tool_calls: list[tuple[str, str]] = []
    tool_events: list[dict] = []
    tool_errors: list[str] = []
    diagnostics: list[str] = []
    usage_records: list[dict] = []
    agent_usage: list[dict] = []
    message_usage: list[dict] = []
    agent_costs: list[float] = []
    message_costs: list[float] = []
    turn_costs: list[float] = []

    for record in records:
        event_type = record.get("type")
        if (
            event_type
            in {
                "agent_start",
                "agent_end",
                "turn_start",
                "turn_end",
                "message_start",
                "message_update",
                "message_end",
                "compaction_start",
                "compaction_end",
                "auto_retry_start",
                "auto_retry_end",
            }
            or event_type in _TOOL_EVENT_TYPES
            or event_type in _ERROR_EVENT_TYPES
        ):
            n_steps += 1

        if event_type == "agent_end":
            messages = record.get("messages")
            if isinstance(messages, list):
                for item in reversed(messages):
                    diagnostic = _message_diagnostic(item)
                    if diagnostic:
                        diagnostics.append(diagnostic)
                    text = _message_text(item)
                    if text:
                        agent_text = text
                        break
            usage = _usage(record)
            if usage:
                agent_usage.append(usage)
            cost = _cost(record)
            if cost:
                agent_costs.append(cost)
        elif event_type == "message_end":
            message = record.get("message")
            diagnostic = _message_diagnostic(message)
            if diagnostic:
                diagnostics.append(diagnostic)
            text = _message_text(message)
            if text:
                message_text = text
            usage = _usage(record)
            if usage:
                message_usage.append(usage)
            cost = _cost(record)
            if cost:
                message_costs.append(cost)
        elif event_type == "turn_end":
            usage = _usage(record)
            if usage:
                usage_records.append(usage)
            cost = _cost(record)
            if cost:
                turn_costs.append(cost)
        elif event_type == "tool_execution_start":
            name = str(record.get("toolName", "tool"))
            tool_events.append(_tool_event(record, name, "started"))
        elif event_type == "tool_execution_update":
            name = str(record.get("toolName", "tool"))
            tool_events.append(_tool_event(record, name, "updated"))
        elif event_type == "tool_execution_end":
            name = str(record.get("toolName", "tool"))
            result = record.get("result")
            result_has_error = isinstance(result, dict) and (
                bool(result.get("isError"))
                or bool(result.get("error"))
                or bool(result.get("errorMessage"))
            )
            failed = (
                bool(record.get("isError")) or bool(record.get("errorMessage")) or result_has_error
            )
            status = "failed" if failed else "completed"
            tool_calls.append((name, status))
            tool_events.append(_tool_event(record, name, status))
            if failed:
                diagnostic = _tool_result_text(result) or f"tool {name} failed"
                tool_errors.append(diagnostic)
        elif event_type in _ERROR_EVENT_TYPES | {
            "auto_retry_start",
            "auto_retry_end",
            "compaction_end",
        }:
            diagnostic = _diagnostic(record)
            if diagnostic:
                diagnostics.append(diagnostic)

    tokens = {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
    }
    selected_usage = usage_records or agent_usage or message_usage
    for usage in selected_usage:
        for key in ("input", "output", "cache_read", "cache_write"):
            value = usage.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                tokens[key] += int(value)
        total = usage.get("total")
        if isinstance(total, int | float) and not isinstance(total, bool):
            tokens["total"] += int(total)
    if tokens["total"] == 0:
        tokens["total"] = tokens["input"] + tokens["output"]

    if agent_costs:
        cost_reported = sum(agent_costs)
    elif turn_costs:
        cost_reported = sum(turn_costs)
    else:
        cost_reported = sum(message_costs)

    text = agent_text or message_text
    if not records and raw.strip():
        text = raw.strip()

    all_errors = diagnostics + tool_errors
    if rc != 0:
        all_errors.append(f"non-zero exit code {rc}")
    error = "; ".join(dict.fromkeys(all_errors)) or None
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


class PiProvider:
    name = "pi"

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
        return run_pi(
            agent=agent,
            model=stripped_model,
            message=message,
            cwd=cwd,
            log_path=log_path,
            timeout=timeout,
        )

    def system_prompt_for(self, agent: str) -> str | None:
        return _system_prompt_for(agent)

    def discover_models(self) -> list[str]:
        return []


register(PiProvider())
