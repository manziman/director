"""Strict KEY=VALUE parsing for `~/.director/agent/agent.env`.

Service managers do not inherit an interactive shell environment, so the agent
loads this file at startup. It is parsed as data and NEVER evaluated by a
shell — no expansion, no substitution, no quoting semantics beyond stripping
one matching pair of surrounding quotes. Malformed lines are hard errors:
a service that silently drops half its environment is worse than one that
refuses to start with a line number.
"""

from __future__ import annotations

import re
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(ValueError):
    """agent.env is malformed; the message names the offending line."""


def parse_env_text(text: str, *, source: str = "agent.env") -> dict[str, str]:
    env: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise EnvFileError(f"{source}:{lineno}: expected KEY=VALUE, got {raw!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.match(key):
            raise EnvFileError(f"{source}:{lineno}: invalid variable name {key!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def load_env_file(path: Path) -> dict[str, str]:
    """Parse `path` if it exists; a missing file is an empty environment."""
    path = Path(path)
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(), source=str(path))
