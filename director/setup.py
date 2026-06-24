"""Install Director's OpenCode agent definitions into a target repo.

Director drives OpenCode with `--agent <role> --model <tier>`; the agent markdown
supplies the role's system prompt + permissions, and the resolved tier model
comes from config via `--model`. This module renders the bundled agent templates
into <repo>/.opencode/agents/ (a.k.a. the `director sync-agents` step).

Provider auth (Bedrock/OpenRouter keys, the LM Studio endpoint) is the operator's
responsibility — it lives in the user's *global* OpenCode config. We add the
project-local agent files, a starter opencode.json if the repo has none, and seed
a ready-to-edit .director/config.toml from the bundled example (if none exists).
"""

from __future__ import annotations

import importlib.resources as ir
from pathlib import Path

AGENT_FILES = (
    "brainstorm.md",
    "planner.md",
    "explorer.md",
    "test-author.md",
    "executor.md",
    "reviewer.md",
)

# A complete, commented example config ships inside the package. sync-agents seeds
# it to <repo>/.director/config.toml (if missing) so a pip-installed user has a
# ready-to-edit config rather than nothing.
CONFIG_EXAMPLE = "config.example.toml"

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


def _template(name: str) -> str:
    return ir.files("director.agent_templates").joinpath(name).read_text()


def _example_config() -> str:
    return ir.files("director").joinpath(CONFIG_EXAMPLE).read_text()


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


def sync_agents(repo: str | Path) -> list[str]:
    """Render agent_templates/*.md into <repo>/.opencode/agents/. Returns the
    list of files written."""
    repo = Path(repo)
    ensure_director_gitignore(repo)
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

    # Seed a ready-to-edit config from the bundled example — but never clobber an
    # existing config the user may have edited.
    cfg = repo / ".director" / "config.toml"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(_example_config())
        written.append(str(cfg.relative_to(repo)))
    return written
