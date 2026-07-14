"""Interactive `director init`: discover models, prompt for tiers/gates, render TOML.

This module wires the interactive `director init` flow. It discovers available
models by iterating registered providers and collecting their tier strings,
prompts the user to bind each role to a model (or falls back to free-text entry
when discovery is unavailable), prompts for the deterministic gate commands,
and renders a minimal `.director/config.toml`. The renderer is pure and its
output round-trips through `director.config.load_file`.
"""

from __future__ import annotations

from pathlib import Path

import director.claudecode  # noqa: F401 — ensures ClaudeCodeProvider registers
import director.codex  # noqa: F401 — ensures CodexProvider registers
import director.opencode  # noqa: F401 — ensures OpenCodeProvider registers
import director.pi  # noqa: F401 — ensures PiProvider registers
import director.provider as provider
from director.config import ROLES, _user_config_path


def parse_models(text: str) -> list[str]:
    """Parse `opencode models` output into a deduped, ordered list of model ids.

    Lines are stripped; blank lines and lines without a `/` are dropped. The
    first occurrence of each model id is kept and later duplicates discarded.
    """
    seen: set[str] = set()
    models: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if "/" not in stripped:
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        models.append(stripped)
    return models


def discover_models() -> list[str]:
    """Union of all registered providers' discover_models(), deduped in registration order."""
    seen: set[str] = set()
    result: list[str] = []
    for prov in provider.providers():
        for tier in prov.discover_models():
            if tier not in seen:
                seen.add(tier)
                result.append(tier)
    return result


def prompt_model(role: str, models: list[str]) -> str:
    """Prompt the user to bind `role` to a model, looping until a valid choice."""
    if models:
        for i, model in enumerate(models, start=1):
            print(f"  {i}) {model}")
        while True:
            answer = input(f"select model for {role}: ").strip()
            if not answer:
                continue
            try:
                n = int(answer)
            except ValueError:
                print("invalid selection")
                continue
            if 1 <= n <= len(models):
                return models[n - 1]
            print("invalid selection")
    else:
        while True:
            answer = input(f"enter model for {role}: ").strip()
            if answer:
                return answer


def prompt_gate(name: str) -> str:
    """Prompt once for the `name` gate command; blank means skip and is valid."""
    return input(f"command for {name} gate (blank to skip): ").strip()


def prompt_gate_name() -> str:
    """Prompt once for a gate name; blank finishes gate configuration."""
    return input("gate name (blank to finish): ").strip()


def render_config(tiers: dict[str, str], gates: dict[str, str]) -> str:
    """Render a minimal `.director/config.toml` text from tiers and gates."""

    def emit(table: dict[str, str], *, quote_keys: bool = False) -> list[str]:
        lines = []
        for key, value in table.items():
            rendered_key = key
            if quote_keys:
                escaped_key = key.replace("\\", "\\\\").replace('"', '\\"')
                rendered_key = f'"{escaped_key}"'
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{rendered_key} = "{escaped}"')
        return lines

    parts: list[str] = []
    parts.append("[tiers]")
    parts.extend(emit(tiers))
    parts.append("")
    parts.append("[gates]")
    parts.extend(emit(gates, quote_keys=True))
    parts.append("")
    parts.append("# Advanced options (pricing, limits, review) are omitted here.")
    parts.append("# See the bundled config.example.toml for the full schema.")
    return "\n".join(parts) + "\n"


def is_inside_git_repo(start: Path) -> bool:
    """Walk up from `start.resolve()` through each parent; return True if any ancestor contains a `.git` directory or file."""
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return True
        parent = current.parent
        if parent == current:  # reached filesystem root
            break
        current = parent
    return False


def resolve_init_target(repo: str, *, user: bool, local: bool) -> Path:
    """Return the config file path to write based on user/local flags and git detection."""
    if user:
        return _user_config_path()
    elif local:
        return Path(repo) / ".director" / "config.toml"
    else:
        if is_inside_git_repo(Path(repo)):
            return Path(repo) / ".director" / "config.toml"
        return _user_config_path()


def run_init(repo: str, *, user: bool = False, local: bool = False) -> Path:
    """Orchestrate the interactive init flow and write `.director/config.toml`."""
    target = resolve_init_target(repo, user=user, local=local)

    if target.exists():
        answer = input("config.toml exists; overwrite? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted; nothing was written.")
            return target

    models = discover_models()
    if not models:
        print(
            "warning: model discovery was unavailable or returned no models; "
            "falling back to free-text entry."
        )

    tiers: dict[str, str] = {}
    for role in ROLES:
        tiers[role] = prompt_model(role, models)

    gates: dict[str, str] = {}
    while name := prompt_gate_name():
        command = prompt_gate(name)
        if command:
            gates[name] = command

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(tiers, gates))
    return target
