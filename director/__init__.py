"""Director — a model-agnostic decomposition coding harness.

A strong planner tier decomposes a task into atomic, well-specified units with
acceptance tests written first; a cheaper executor tier implements each unit in
an isolated git worktree with a fresh context; deterministic gates (tests, lint,
typecheck, exit codes — never an LLM judge) decide what merges. Roles bind to
model tiers in `.director/config.toml`; nothing here knows "local" vs "cloud".
"""

__version__ = "0.4.0"
