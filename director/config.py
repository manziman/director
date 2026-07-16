"""Load and resolve `.director/config.toml` (the active profile).

Roles → tier model strings, deterministic gate commands, per-model pricing, and
run limits. Everything the orchestrator knows about a "model" comes from here;
switching executor models is a config edit, never a code change.

Repo-local tables deep-merge over user defaults except ``[gates]``. Gates are
repo-local only: user-level gate commands are ignored, and an omitted repo table
means that the repository has no integration gates.
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
    gates: dict[str, str]  # gate name -> command ("" = skip); insertion-ordered
    pricing: dict[str, dict]  # "provider/model" -> {"input": $/Mtok, "output": $/Mtok}
    limits: dict  # node_timeout_secs, cost_ceiling_usd, max_attempts
    sampling: dict  # role -> {temperature, top_p, top_k}
    review: dict  # two-stage review knobs (Phase 2.5)
    repository: dict = field(default_factory=dict)  # repository-level settings
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


def _user_config_path() -> Path:
    """Return the path to the user-level config file."""
    return Path.home() / ".director" / "config.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively deep-merge *override* into a copy of *base*.

    Returns a NEW dict; does NOT mutate either input. For keys present in both
    whose values are dicts the merge recurses (arbitrary depth).  Scalars,
    lists/arrays, and dict-vs-non-dict all REPLACE wholesale — arrays are never
    concatenated or element-merged.
    """
    result = {**base}
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(base[key], value)
        else:
            result[key] = value
    return result


def _preserve_legacy_repository_settings(data: dict) -> dict:
    """Copy legacy non-command gate settings into their repository table.

    This runs per config layer before user gate commands are discarded and the
    layers merge, preserving a user-level legacy ignore default.
    """
    gates = data.get("gates", {})
    repository = data.get("repository", {})
    legacy_ignore = gates.get("ignore") if isinstance(gates, dict) else None
    if (
        legacy_ignore is None
        or isinstance(legacy_ignore, str)
        or not isinstance(repository, dict)
        or "ignore" in repository
    ):
        return data

    migrated = {**data}
    migrated["repository"] = {**repository, "ignore": legacy_ignore}
    return migrated


def _ensure_builtin_providers_registered():
    """Import built-in providers and ensure the current registry has their keys."""
    from director import claudecode, codex, opencode, pi, provider

    for provider_cls in (
        claudecode.ClaudeCodeProvider,
        codex.CodexProvider,
        opencode.OpenCodeProvider,
        pi.PiProvider,
    ):
        if provider.resolve(provider_cls.name) is None:
            provider.register(provider_cls())
    return provider


def _provider_name(value: str) -> str:
    return value.split("/", 1)[0]


def _unknown_provider_error(*, label: str, value: str, known: list[str]) -> ValueError:
    name = _provider_name(value)
    return ValueError(
        f"unknown provider {name!r} in {label} {value!r}. "
        f"Prefix the tier with the tool that runs it, e.g. 'opencode/{value}'. "
        f"Known providers: {', '.join(known)}"
    )


def _validate_provider_keys(tiers: dict[str, str], pricing: dict[str, dict]) -> None:
    provider = _ensure_builtin_providers_registered()
    known = sorted(p.name for p in provider.providers())

    for tier in tiers.values():
        if provider.resolve(_provider_name(tier)) is None:
            raise _unknown_provider_error(label="tier", value=tier, known=known)

    for tier in pricing:
        if provider.resolve(_provider_name(tier)) is None:
            raise _unknown_provider_error(label="pricing key", value=tier, known=known)


def _build_config(data: dict, path: Path) -> Config:
    """Validate tiers completeness and construct a Config from parsed data."""
    data = _preserve_legacy_repository_settings(data)
    tiers = data.get("tiers", {})
    missing = [r for r in ROLES if r not in tiers]
    if missing:
        raise ValueError(f"[tiers] in {path} is missing roles: {', '.join(missing)}")
    pricing = data.get("pricing", {})
    _validate_provider_keys(tiers, pricing)

    # Gate names are user-defined; string values are executable commands.
    raw_gates = data.get("gates", {})
    gates = {key: value for key, value in raw_gates.items() if isinstance(value, str)}
    repository = dict(data.get("repository", {}))

    return Config(
        path=path,
        tiers=tiers,
        gates=gates,
        pricing=pricing,
        limits=data.get("limits", {}),
        sampling=data.get("sampling", {}),
        review=data.get("review", {}),
        repository=repository,
        target=data.get("target", {}),
    )


def load(repo: Path) -> Config:
    """Load the active config, merging user-level then repo-level configs."""
    user_path = _user_config_path()
    repo_path = Path(repo) / ".director" / "config.toml"

    if not user_path.exists() and not repo_path.exists():
        raise FileNotFoundError(
            f"{repo_path} not found. Run `director init` to create it interactively."
        )

    data: dict = {}
    if user_path.exists():
        with user_path.open("rb") as f:
            data = _preserve_legacy_repository_settings(tomllib.load(f))
        # Commands in the cross-repository user config are never repository gates.
        data["gates"] = {}
    if repo_path.exists():
        with repo_path.open("rb") as f:
            repo_data = _preserve_legacy_repository_settings(tomllib.load(f))
        data = _deep_merge(data, repo_data)
        # Gates are repo-local only. The repository table is a complete set,
        # and omission configures no gates.
        data["gates"] = repo_data.get("gates", {})

    active_path = repo_path if repo_path.exists() else user_path
    return _build_config(data, active_path)


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

    return _build_config(data, path)
