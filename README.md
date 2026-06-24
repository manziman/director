# director

**A model-agnostic decomposition coding harness — a thin orchestrator over [OpenCode](https://opencode.ai).**

director tests one hypothesis:

> A strong **planner** model decomposes a coding task into small, atomic, well-specified
> units with acceptance tests written *first*. A cheaper **executor** model — local or
> low-cost cloud — implements each unit in an isolated, fresh context. **Deterministic
> gates** (tests, lint, typecheck — exit codes, never an LLM's opinion) decide what
> merges. This cuts token cost dramatically with minimal quality loss.

It is **model-agnostic by construction**: roles (`planner`, `executor`, `reviewer`, …)
bind to `provider/model` strings in config. Switching the executor from a local 27B to
a frontier model — or anything in between — is a one-line config edit, never a code
change. director drives OpenCode headlessly, so it inherits OpenCode's 75+ providers.

> **Status:** beta. Validated end-to-end (plan → run → bench) under local, cheap-cloud,
> and all-frontier executor tiers.

---

## Install

director is a pure-standard-library Python CLI (no dependencies), so it installs anywhere
Python 3.11+ runs:

```bash
uv tool install director-cli      # recommended
# or
pipx install director-cli
# or
pip install director-cli
```

### Prerequisites (runtime)

director orchestrates other tools rather than replacing them, so it needs:

- **Python ≥ 3.11**
- **git** on `PATH` (isolation is real git worktrees + branches)
- **[OpenCode](https://opencode.ai)** on `PATH` (the agent runtime director drives)
- **Provider auth** configured in OpenCode (`opencode auth`): your planner/executor
  model providers — e.g. Anthropic/Bedrock, OpenRouter, or a local OpenAI-compatible
  endpoint such as LM Studio for the `local-first` profile.

director never manages provider keys itself — that lives in your OpenCode config.

---

## Quickstart

```bash
cd your-repo

# 1. Install director's role agents into .opencode/ and seed .director/config.toml
director sync-agents

# 2. Edit .director/config.toml — bind roles to models, set your gate commands.
#    (sync-agents seeded it from the bundled, fully-commented example.)
$EDITOR .director/config.toml

# 3. Plan: brainstorm → spec → test-gated task DAG (two approval gates)
director plan "Add a --json flag to the export command"

#    …review .director/spec.md, then continue; review the plan, then continue:
director plan --continue            # after approving the spec
director plan --continue            # after approving the plan + failing tests

# 4. Run the DAG: isolated worktree per node, deterministic gates, auto-merge
director run

# 5. Inspect
director status
```

Unattended? Let the planner self-critique at each gate instead of pausing for you:

```bash
director plan "…" --auto              # planner self-critiques at each gate
director plan "…" --auto --no-critique  # gates auto-pass, fully hands-off
director run
```

---

## Commands

| Command | What it does |
| --- | --- |
| `director plan "<task>" [--auto] [--no-critique] [--continue]` | Brainstorm → spec → test-gated task DAG, with two artifact-based approval gates. |
| `director run [--parallel N] [--max-attempts K]` | Execute the DAG: each node in an isolated git worktree, gated by tests/lint/typecheck, auto-merged on pass; escalates a stuck node one tier up. |
| `director status` | Per-node progress, attempts, cost, and the executor-tier completion rate. |
| `director bench "<task>" --profiles a,b,c` | Run the **same** task (same frozen acceptance tests) across profile variants and diff cost / quality / wall-time. |
| `director sync-agents` | (Re)install the role agents into `<repo>/.opencode` and seed `.director/config.toml`. |

All state lives under `.director/` (resumable, debuggable): `plan.json`, `state.json`,
`costs.jsonl`, `metrics.jsonl`, per-call `logs/`, and `bench/`.

## Configuration

`director sync-agents` seeds `.director/config.toml` from a complete, commented example
(also at [`director/config.example.toml`](director/config.example.toml)). A config is
just roles → `provider/model` strings, the deterministic gate commands, per-model
pricing, and run limits — the example shows how to bind the executor tier to a local
model (≈ $0 implementation), a low-cost cloud model (zero local infra), or a frontier
model (the expensive baseline). See [`director/README.md`](director/README.md) for the
full architecture (gates, two-stage review, red-green hardening, metrics).

### Comparing setups with `bench`

`director bench` plans a task once, then runs the **same** frozen acceptance tests under
several config variants to compare cost/quality/wall-time. Create the variants as
`.director/profiles/<name>.toml` (copy your `config.toml` and change the executor tier in
each), then:

```bash
director bench "<task>" --profiles all-frontier,cheap-cloud,local-first
```

## For agents & scripting

director is built to be driven by humans *or* by another agent:

- **Deterministic, non-interactive:** `--auto --no-critique` runs plan→run with no
  prompts; every merge decision is an exit code, never a chat.
- **Machine-readable output:** `.director/metrics.jsonl` (per-node + per-run records)
  and `.director/bench/summary.json` are stable JSON for downstream tooling.
- **Resumable:** re-running `plan --continue` / `run` picks up from `.director/` state.

---

## Development

```bash
uv sync                       # create the dev environment
uv run python -m unittest discover -s tests -q   # tests
uvx ruff check . && uvx ruff format --check .     # lint + format
uv build                      # build the wheel/sdist
```

Releases are automated with [python-semantic-release](https://python-semantic-release.readthedocs.io/)
on merge to `main` (conventional-commit messages drive the version bump, changelog, and
PyPI publish via Trusted Publishing). See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

[MIT](LICENSE) © Christopher Manzi. The ported TDD/review *discipline* is adapted from
[obra/superpowers](https://github.com/obra/superpowers) (MIT).
