"""Headless Claude Code driver.

Wraps `claude -p <message>` with bundled system-prompt templates and parses the
JSON / NDJSON output into a structured RunResult.  Stdlib only."""

from __future__ import annotations

import contextlib
import importlib.resources as ir
import json
import subprocess
from pathlib import Path

from director.opencode import _CLEAN_ENV, RunResult

# --------------------------------------------------------------------------- #
# system_prompt_for
# --------------------------------------------------------------------------- #


def system_prompt_for(agent: str) -> str:
    """Return the bundled template for *agent* with YAML frontmatter stripped."""
    filename = agent.replace("_", "-") + ".md"
    tpl = ir.files("director.agent_templates").joinpath(filename).read_text()
    stripped_tpl = tpl.lstrip("\n")
    lines = stripped_tpl.splitlines()
    if lines and lines[0] == "---":
        closer = -1
        for i in range(1, len(lines)):
            if lines[i] == "---":
                closer = i
                break
        if closer != -1:
            return "\n".join(lines[closer + 1 :]).strip()
    return tpl.strip()


# --------------------------------------------------------------------------- #
# _bare_model
# --------------------------------------------------------------------------- #


def _bare_model(model: str) -> str:
    """Drop the provider prefix (everything up to and including the first `/`)."""
    idx = model.find("/")
    if idx == -1:
        return model
    return model[idx + 1 :]


# --------------------------------------------------------------------------- #
# run_claude
# --------------------------------------------------------------------------- #


def run_claude(
    *,
    agent: str,
    model: str,
    message: str,
    cwd: str | Path,
    log_path: str | Path,
    timeout: int,
) -> RunResult:
    """Invoke Claude Code headlessly.  Never raises on CLI / model failure."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    err_path = log_path.with_suffix(log_path.suffix + ".stderr")

    cmd = [
        "claude",
        "-p",
        message,
        "--output-format",
        "json",
        "--model",
        _bare_model(model),
        "--append-system-prompt",
        system_prompt_for(agent),
        "--dangerously-skip-permissions",
    ]

    timed_out = False
    with open(log_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=err, env=_CLEAN_ENV)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(AttributeError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait()
            rc = 124
            timed_out = True

    return _parse_claude(log_path, rc, timed_out)


# --------------------------------------------------------------------------- #
# _parse_claude
# --------------------------------------------------------------------------- #


def _parse_claude(log_path: Path, rc: int, timed_out: bool) -> RunResult:
    """Shape-tolerant parser for Claude Code JSON / NDJSON output."""
    text_parts: list[str] = []
    tokens = {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
    }
    n_steps: int | None = None
    tool_calls: list[tuple[str, str]] = []
    tool_events: list[dict] = []
    error: str | None = None

    raw = log_path.read_text(errors="replace")
    stripped = raw.strip()

    # Try single JSON object first.
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
        except json.JSONDecodeError:
            # Fall through to NDJSON line-by-line.
            pass

    if not records:
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except json.JSONDecodeError:
                continue

    # --- aggregate across all parsed records ---------------------------------
    total_cost = 0.0
    has_num_turns = False
    assistant_count = 0

    for rec in records:
        # -- text -------------------------------------------------------------
        result_val = rec.get("result")
        if isinstance(result_val, str):
            text_parts.append(result_val)

        msg = rec.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    t = seg.get("text", "")
                    if isinstance(t, str):
                        text_parts.append(t)

        # -- tool calls inside message.content ---------------------------------
        if isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "tool_use":
                    _collect_tool(seg, tool_calls, tool_events)

        # -- top-level tool records -------------------------------------------
        rec_type = rec.get("type", "")
        if rec_type in ("tool_use", "tool_result"):
            name = rec.get("name") or rec.get("tool", "?")
            status = rec.get("status") or rec.get("state", "?")
            _collect_tool_entry(str(name), str(status), rec, tool_calls, tool_events)

        # -- tokens -----------------------------------------------------------
        usage = rec.get("usage")
        if not isinstance(usage, dict):
            msg_usage = (msg if isinstance(msg, dict) else {}).get("usage")
            if isinstance(msg_usage, dict):
                usage = msg_usage
        if isinstance(usage, dict):
            tokens["input"] += int(usage.get("input_tokens") or 0)
            tokens["output"] += int(usage.get("output_tokens") or 0)
            cr = usage.get("cache_read_input_tokens")
            if cr is not None:
                tokens["cache_read"] += int(cr)
            cw = usage.get("cache_creation_input_tokens")
            if cw is not None:
                tokens["cache_write"] += int(cw)
            reasoning_val = usage.get("reasoning") or usage.get("reasoning_tokens")
            if reasoning_val is not None:
                tokens["reasoning"] += int(reasoning_val)

        # -- cost -------------------------------------------------------------
        tc = rec.get("total_cost_usd")
        if tc is not None:
            total_cost += float(tc)

        # -- n_steps ----------------------------------------------------------
        nt = rec.get("num_turns")
        if nt is not None and not has_num_turns:
            n_steps = int(nt)
            has_num_turns = True

        if rec_type == "assistant":
            assistant_count += 1

        # -- error ------------------------------------------------------------
        if rec.get("is_error"):
            error = str(rec.get("error") or rec.get("subtype") or "error occurred")
        elif rec_type in ("error",) and (rec.get("error") or rec.get("subtype")):
            error = str(rec.get("error") or rec.get("subtype"))

    # -- finalize tokens ------------------------------------------------------
    if tokens["total"] == 0:
        total_tok = None
        for rec in records:
            usage = rec.get("usage")
            if not isinstance(usage, dict):
                msg_usage = (
                    rec.get("message", {}) if isinstance(rec.get("message"), dict) else {}
                ).get("usage")
                if isinstance(msg_usage, dict):
                    usage = msg_usage
            if isinstance(usage, dict):
                tt = usage.get("total_tokens")
                if tt is not None:
                    total_tok = int(tt)
        if total_tok is not None:
            tokens["total"] = total_tok
        else:
            tokens["total"] = tokens["input"] + tokens["output"]

    # -- finalize n_steps -----------------------------------------------------
    if n_steps is None:
        n_steps = assistant_count if assistant_count > 0 else 0

    # -- finalize error -------------------------------------------------------
    text = "".join(text_parts).strip()
    if rc != 0 and not text and error is None:
        error = f"non-zero exit code {rc}"

    return RunResult(
        returncode=rc,
        text=text,
        tokens=tokens,
        cost_reported=total_cost,
        n_steps=n_steps,
        tool_calls=tool_calls,
        tool_events=tool_events,
        error=error,
        timed_out=timed_out,
        log_path=str(log_path),
    )


def _collect_tool(seg: dict, tool_calls: list[tuple[str, str]], tool_events: list[dict]) -> None:
    name = seg.get("name", "?")
    status = "?"
    _collect_tool_entry(str(name), status, seg, tool_calls, tool_events)


def _collect_tool_entry(
    name: str,
    status: str,
    record: dict,
    tool_calls: list[tuple[str, str]],
    tool_events: list[dict],
) -> None:
    tool_calls.append((name, status))
    blob = json.dumps(record, default=str)[:2000].lower()
    tool_events.append({"name": name.lower(), "status": status, "blob": blob})
