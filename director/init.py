"""Interactive `director init`: discover models, prompt for tiers/gates, render TOML.

This module wires the interactive `director init` flow. It discovers available
models by shelling out to `opencode models`, prompts the user to bind each role
to a model (or falls back to free-text entry when discovery is unavailable),
prompts for the deterministic gate commands, and renders a minimal
`.director/config.toml`. The renderer is pure and its output round-trips through
`director.config.load_file`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from director.config import ROLES


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
    """Run `opencode models` and parse its output; return [] on any failure."""
    try:
        result = subprocess.run(
            ["opencode", "models"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return parse_models(result.stdout)


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


def render_config(tiers: dict[str, str], gates: dict[str, str]) -> str:
    """Render a minimal `.director/config.toml` text from tiers and gates."""

    def emit(table: dict[str, str]) -> list[str]:
        lines = []
        for key, value in table.items():
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        return lines

    parts: list[str] = []
    parts.append("[tiers]")
    parts.extend(emit(tiers))
    parts.append("")
    parts.append("[gates]")
    parts.extend(emit(gates))
    parts.append("")
    parts.append("# Advanced options (pricing, limits, review) are omitted here.")
    parts.append("# See the bundled config.example.toml for the full schema.")
    return "\n".join(parts) + "\n"


def run_init(repo: str) -> Path:
    """Orchestrate the interactive init flow and write `.director/config.toml`."""
    cfg_path = Path(repo) / ".director" / "config.toml"

    if cfg_path.exists():
        answer = input("config.toml exists; overwrite? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted; nothing was written.")
            return cfg_path

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
    for name in ("test", "lint", "typecheck"):
        gates[name] = prompt_gate(name)

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(render_config(tiers, gates))
    return cfg_path
