"""Load and resolve `.director/config.toml` (the active profile).

Roles → tier model strings, deterministic gate commands, per-model pricing, and
run limits. Everything the orchestrator knows about a "model" comes from here;
switching executor models is a config edit, never a code change.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROLES = ("planner", "test_author", "executor", "explorer", "reviewer", "escalation")


@dataclass
class Config:
    path: Path
    tiers: dict[str, str]  # role -> "provider/model"
    gates: dict[str, str]  # "test"|"lint"|"typecheck" -> command ("" = skip)
    pricing: dict[str, dict]  # "provider/model" -> {"input": $/Mtok, "output": $/Mtok}
    limits: dict  # node_timeout_secs, cost_ceiling_usd, max_attempts
    sampling: dict  # role -> {temperature, top_p, top_k}
    local: dict  # providers.local: base_url, api_key
    review: dict  # two-stage review knobs (Phase 2.5)
    # Optional declared target stack; currently declaration-only (available on Config);
    # recon remains the primary signal to the planner.
    target: dict = field(default_factory=dict)

    # --- convenience resolvers ----------------------------------------------
    def model_for(self, role: str) -> str:
        if role not in self.tiers:
            raise KeyError(f"role '{role}' not bound in [tiers] of {self.path}")
        return self.tiers[role]

    def price(self, model: str) -> dict:
        return self.pricing.get(model, {"input": 0.0, "output": 0.0})

    @property
    def node_timeout(self) -> int:
        return int(self.limits.get("node_timeout_secs", 900))

    @property
    def cost_ceiling(self) -> float:
        return float(self.limits.get("cost_ceiling_usd", 0.0))  # 0 = no ceiling

    @property
    def max_attempts(self) -> int:
        return int(self.limits.get("max_attempts", 3))

    @property
    def flake_runs(self) -> int:
        """How many times to run a node's tests on the success path (Phase 3 flake
        control). Default 2 = run twice; a mismatch between runs fails the node as
        flaky. 1 disables the extra run."""
        return max(1, int(self.limits.get("flake_runs", 2)))

    # --- two-stage review (Phase 2.5) ---------------------------------------
    @property
    def stage_two_file_threshold(self) -> int:
        """Stage-two (code-quality) review fires when a node escalated OR its diff
        touched MORE than this many files. Default 3 (configurable)."""
        return int(self.review.get("stage_two_file_threshold", 3))

    @property
    def stage_one_llm(self) -> bool:
        """Run the optional explorer-tier spec-compliance check in stage one.
        Off by default — the deterministic node gate already enforces the
        contract; this is a cheap belt-and-suspenders LLM pass."""
        return bool(self.review.get("stage_one_llm", False))

    @property
    def stage_two_enabled(self) -> bool:
        return bool(self.review.get("stage_two", True))

    # --- target convenience properties --------------------------------------
    @property
    def target_language(self):
        return self.target.get("language") or None

    @property
    def target_test_framework(self):
        return self.target.get("test_framework") or None

    @property
    def target_toolchain(self):
        return self.target.get("toolchain") or None


def load(repo: Path) -> Config:
    """Load the active config from <repo>/.director/config.toml."""
    path = Path(repo) / ".director" / "config.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `director init` to create it interactively."
        )
    return load_file(path)


def load_file(path: Path) -> Config:
    """Load a Config from a specific TOML path (e.g. a profile). Used by
    `director bench` to load each profile WITHOUT swapping the active
    config.toml — run_plan/run_job take a Config object, so bench never has to
    mutate (and thereby dirty) the tracked config.toml on disk."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found.")
    with path.open("rb") as f:
        data = tomllib.load(f)

    tiers = data.get("tiers", {})
    missing = [r for r in ROLES if r not in tiers]
    if missing:
        raise ValueError(f"[tiers] in {path} is missing roles: {', '.join(missing)}")

    return Config(
        path=path,
        tiers=tiers,
        gates=data.get("gates", {}),
        pricing=data.get("pricing", {}),
        limits=data.get("limits", {}),
        sampling=data.get("sampling", {}),
        local=data.get("providers", {}).get("local", {}),
        review=data.get("review", {}),
        target=data.get("target", {}),
    )
