"""Shared, dependency-free provider primitives and registry.

Built-in adapters include claude-code, codex, opencode, and pi. This module imports
NOTHING from the `director` package to avoid future import cycles.
Allowed imports: stdlib only (Python >= 3.11).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

# --------------------------------------------------------------------------- #
# _CLEAN_ENV — provider subprocess environment (byproducts handled by gate's ignore matcher)
# --------------------------------------------------------------------------- #

_CLEAN_ENV = {**os.environ}
_CLEAN_ENV.pop("PYTHONDONTWRITEBYTECODE", None)


# --------------------------------------------------------------------------- #
# RunResult — structured result from a provider invocation
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
# Provider — protocol for provider implementations
# --------------------------------------------------------------------------- #


class Provider(Protocol):
    name: str

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

        Returns ready-to-paste "<provider>/<model-ref>" tier strings (e.g.
        "opencode/anthropic/claude-x" or "claude-code/opus").  Returns an
        empty list [] when the provider's source is unavailable.  MUST NEVER raise.
        """
        ...


# --------------------------------------------------------------------------- #
# Registry — global provider-name → provider mapping
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, Provider] = {}


def register(prov: Provider) -> None:
    if prov.name in _REGISTRY:
        raise ValueError(
            f"Provider {prov.name!r} already registered by "
            f"{_REGISTRY[prov.name].__class__.__name__}; "
            f"cannot also be used by {prov.__class__.__name__}"
        )
    _REGISTRY[prov.name] = prov


def resolve(provider: str) -> Provider | None:
    return _REGISTRY.get(provider)


def provider_for_model(model: str) -> Provider | None:
    provider = model.split("/", 1)[0]
    return resolve(provider)


def providers() -> list[Provider]:
    """Return registered provider instances in stable registration order."""
    return list(_REGISTRY.values())
