"""Install Director's OpenCode agent definitions into a target repo.

Renders role agent prompts into .opencode/agents/ ONLY when an OpenCode-owned
provider is configured (in the passed Config or on-disk configuration). With no
config, a claude-code-only config, or a malformed config, only .director/.gitignore
is written and nothing under .opencode/ is created. Provider auth remains the
operator's responsibility in the user's global OpenCode config.
"""

from __future__ import annotations

import importlib.resources as ir
from pathlib import Path

from director import config
from director.config import Config
from director.opencode import OPENCODE_PROVIDERS

AGENT_FILES = (
    "brainstorm.md",
    "planner.md",
    "explorer.md",
    "test-author.md",
    "executor.md",
    "reviewer.md",
)

# Runtime artifacts director writes into <repo>/.director/. These must never be
# committed: director's own commits use `git add -A` (to capture whatever the
# executor created in the allowlist), which in a repo without this ignore file
# would sweep these throwaway files into the job branch — then a later run that
# rewrites them leaves a dirty TRACKED file, which makes `git checkout` refuse
# (this bit `director bench` live). config.toml + profiles/ stay tracked.
_DIRECTOR_GITIGNORE = """\
# director runtime artifacts (generated per run). config.toml + profiles/ stay tracked.
state.json
plan.json
plan_stage.json
spec.md
recon.md
costs.jsonl
metrics.jsonl
logs/
bench/
worktrees/
"""


def _opencode_selected(tiers: dict[str, str]) -> bool:
    return any(tier.split("/", 1)[0] in OPENCODE_PROVIDERS for tier in tiers.values())


def _template(name: str) -> str:
    return ir.files("director.agent_templates").joinpath(name).read_text()


def ensure_director_gitignore(repo: str | Path) -> None:
    """Seed <repo>/.director/.gitignore so director's `git add -A` commits never
    sweep its own runtime files into the repo. Idempotent; safe to call on every
    plan/run. (Does not retroactively untrack files already committed in a repo
    that ran director before this existed — for those, `git rm --cached` once.)"""
    fdir = Path(repo) / ".director"
    fdir.mkdir(parents=True, exist_ok=True)
    gi = fdir / ".gitignore"
    current = gi.read_text() if gi.exists() else None
    if current != _DIRECTOR_GITIGNORE:
        gi.write_text(_DIRECTOR_GITIGNORE)


def sync_agents(repo: str | Path, cfg: Config | None = None) -> list[str]:
    """Render agent templates into <repo>/.opencode/agents/ when an OpenCode-owned
    provider is configured. Returns the list of repo-relative paths written."""
    repo = Path(repo)
    ensure_director_gitignore(repo)

    # Resolve whether an OpenCode provider is selected.
    if cfg is not None:
        selected = _opencode_selected(cfg.tiers)
    else:
        toml_path = repo / ".director" / "config.toml"
        if toml_path.exists():
            try:
                loaded = config.load_file(toml_path)
                selected = _opencode_selected(loaded.tiers)
            except Exception:
                selected = False
        else:
            selected = False

    if not selected:
        return []

    agents_dir = repo / ".opencode" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in AGENT_FILES:
        dest = agents_dir / name
        dest.write_text(_template(name))
        written.append(str(dest.relative_to(repo)))

    # Starter opencode.json only if the repo has none (never clobber).
    oc = repo / ".opencode" / "opencode.json"
    if not oc.exists():
        oc.write_text(_template("opencode.json"))
        written.append(str(oc.relative_to(repo)))
    return written
