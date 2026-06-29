"""Shared, dependency-free runtime primitives and registry.

This module imports NOTHING from the `director` package to avoid future import cycles.
Allowed imports: stdlib only (Python >= 3.11).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

# --------------------------------------------------------------------------- #
# _CLEAN_ENV — runtime subprocess environment (byproducts handled by gate's ignore matcher)
# --------------------------------------------------------------------------- #

_CLEAN_ENV = {**os.environ}
_CLEAN_ENV.pop("PYTHONDONTWRITEBYTECODE", None)


# --------------------------------------------------------------------------- #
# RunResult — structured result from a runtime invocation
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    returncode: int = 0
    text: str = ""
    tokens: dict = field(default_factory=dict)
    cost_reported: float = 0.0
    n_steps: int = 0
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    tool_events: list[dict] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None and not self.timed_out


# --------------------------------------------------------------------------- #
# Runtime — protocol for runtime implementations
# --------------------------------------------------------------------------- #


class Runtime(Protocol):
    name: str
    providers: frozenset[str]

    def run(
        self,
        *,
        agent: str,
        model: str,
        message: str,
        cwd: str,
        log_path: str,
        timeout: float,
    ) -> RunResult: ...

    def system_prompt_for(self, agent: str) -> str | None: ...

    def discover_models(self) -> list[str]:
        """Additive, init-time-only convenience hook (NOT used by resolution).

        Returns ready-to-paste "<provider>/<model>" tier strings (e.g.
        "opencode/anthropic/claude-x" or "claude-code/opus").  Returns an
        empty list [] when the runtime's source is unavailable.  MUST NEVER raise.
        """
        ...


# --------------------------------------------------------------------------- #
# Registry — global provider-segment → runtime mapping
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, Runtime] = {}


def register(rt: Runtime) -> None:
    for provider in rt.providers:
        if provider in _REGISTRY:
            raise ValueError(
                f"Provider {provider!r} already claimed by "
                f"{_REGISTRY[provider].name!r}; cannot also be used by {rt.name!r}"
            )
    for provider in rt.providers:
        _REGISTRY[provider] = rt


def resolve(provider: str) -> Runtime | None:
    return _REGISTRY.get(provider)


def runtime_for_model(model: str) -> Runtime | None:
    provider = model.split("/", 1)[0]
    return resolve(provider)


def runtimes() -> list[Runtime]:
    """Return the unique registered runtime instances in stable registration order."""
    return list(dict.fromkeys(_REGISTRY.values()))
