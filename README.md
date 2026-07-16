# director

**A model-agnostic decomposition coding harness — a thin orchestrator over agent providers ([OpenCode](https://opencode.ai), Claude Code, Codex, …).**

director tests one hypothesis:

> A strong **planner** model decomposes a coding task into small, atomic, well-specified
> units with acceptance tests written *first*. A cheaper **executor** model — local or
> low-cost cloud — implements each unit in an isolated, fresh context. **Deterministic
> gates** (user-defined commands; exit codes, never an LLM's opinion) decide what
> merges. This cuts token cost dramatically with minimal quality loss.

It is **model-agnostic by construction**: roles (`planner`, `executor`, `reviewer`, …)
bind to `provider/model-ref` strings in config. The provider is the tool director
drives (`opencode`, `claude-code`, or `codex`); the rest of the tier string is whatever that
tool expects. Switching the executor from a local 27B to a frontier model — or
anything in between — is a one-line config edit, never a code change.

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

### Prerequisites (providers)

director orchestrates other tools rather than replacing them, so it needs:

- **Python ≥ 3.11**
- **git** on `PATH` (isolation is real git worktrees + branches)
- **[OpenCode](https://opencode.ai)** on `PATH`, if you use an `opencode/*` tier
- **Claude Code** (`claude`) on `PATH`, if you use a `claude-code/*` tier
- **Codex** (`codex`) on `PATH`, if you use a `codex/*` tier (e.g. `codex/gpt-5-codex`; runs via the OpenAI Codex CLI)
- **Pi** (`pi`) on `PATH`, if you use a `pi/<provider>/<model>` tier (install with `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`)
- **Provider auth**: if you use OpenCode tiers, configure auth in OpenCode via `opencode auth`; Claude Code tiers use the Claude Code CLI's own auth; Codex tiers use the Codex CLI's own auth; Pi uses its normal BYOK provider environment variables

director never manages model-provider keys itself — each tool handles its own credentials.

Pi tiers use the exact `pi/<provider>/<model>` form, such as
`pi/anthropic/claude-sonnet-4-5`, `pi/openai/gpt-5.5`, or `pi/groq/<model>`.
Director does not install Pi, store Pi credentials, discover Pi models, manage Pi
extensions, or resume Pi sessions; v1 discovery returns no models, so configure tiers
explicitly. Pricing can be added with `[pricing."pi/anthropic/claude-sonnet-4-5"]`.

**Manual Pi smoke test (requires a real provider key; not part of automated tests):**
install Pi, export the applicable provider credential such as `ANTHROPIC_API_KEY`, set
`executor = "pi/anthropic/claude-sonnet-4-5"` in `.director/config.toml`, run a small
Director command, and inspect the attempt logs under `.director/logs/`.

---

## Quickstart

```bash
cd your-repo

# 1. Create .director/config.toml interactively — director init asks which model to
#    use per role and optionally configures gates for this repository.
director init
#    (See director/config.example.toml for the full/advanced schema if you want to
#     hand-tune beyond what init prompts for.)
$EDITOR .director/config.toml

# 2. Install director's role agents into .opencode/ (+ gitignore, starter opencode.json)
#    — written only when the opencode provider is configured
director sync-agents

# 3. Plan: brainstorm → spec → test-gated task DAG (two approval gates)
director plan "Add a --json flag to the export command"

#    …review .director/spec.md, then continue; review the plan, then continue:
director plan --continue            # after approving the spec
director plan --continue            # after approving the plan + failing tests

# 4. Run the DAG: isolated worktree per node, deterministic gates, auto-merge
director run

# 5. Inspect
director status

# …or watch it live in a browser (read-only dashboard, run from another terminal)
director ui --open
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
| `director run [--parallel N] [--max-attempts K]` | Execute the DAG: each node runs in an isolated git worktree behind its acceptance test; configured repository gates run after merge. Escalates a stuck node one tier up. |
| `director status` | Per-node progress, attempts, cost, and the executor-tier completion rate. |
| `director ui [--port 8642] [--host 127.0.0.1] [--open]` | Live read-only web dashboard: planning-stage progress (recon → spec → gates → ready, with recon/spec viewers), then the DAG animating as nodes run/pass/fail, with per-node detail, logs, and cost/metrics — all read from `.director/`. Localhost-only by default; `--host 0.0.0.0` exposes unauthenticated read access (specs, logs) to your network. |
| `director auto "<task>" [--input FILE\|-] [--open] [--hold] [--max-cost N] [--force]` | One-shot autonomous mode: plan with self-critiqued gates (no pauses) → run, serving the live dashboard from the same process. Task from a positional string, `--input FILE`, or `--input -` (stdin); resumes an interrupted job, erroring on a changed task unless `--force` replans. `--open` implies `--hold` (dashboard stays up after the run until Ctrl-C). |
| `director bench "<task>" --profiles a,b,c` | Run the **same** task (same frozen acceptance tests) across profile variants and diff cost / quality / wall-time. |
| `director init [--repo .]` | Interactively create config — asks which model to use per role and, for repo-local config, optionally asks for gate commands. |
| `director sync-agents` | (Re)install the role agents into `<repo>/.opencode/` (plus gitignore, starter `opencode.json`) — only when an `opencode/*` tier is configured. |

All state lives under `.director/` (resumable, debuggable): `plan.json`, `state.json`,
`costs.jsonl`, `metrics.jsonl`, per-call `logs/`, and `bench/`.

## Configuration

`director init` interactively creates config — it asks which model to use for each
role and, for repo-local config, optionally collects deterministic gate commands.
A config contains roles → `provider/model-ref` strings, per-model pricing, and run
limits; repo-local config may also contain arbitrary user-defined gate commands.
Director has no fixed gate vocabulary. For the full/advanced schema, see
the complete, commented [`director/config.example.toml`](director/config.example.toml):
it shows how to bind the executor tier to a local
model (≈ $0 implementation), a low-cost cloud model (zero local infra), or a frontier
model (the expensive baseline). See [`director/README.md`](director/README.md) for the
full architecture (gates, two-stage review, red-green hardening, metrics).

### Two-level configuration

director supports two config levels that are combined at runtime:

- **User-level defaults** live at `~/.director/config.toml`. These apply across all
  repositories on your machine.
- **Repo-local overrides** live at `<repo>/.director/config.toml`. These apply only to
  the current repository and take precedence over user-level settings.

The repo file deep-merges over the user file: nested tables merge recursively, while
scalars and arrays replace wholesale. `[gates]` is repo-local only: gate commands in
the user-level file are ignored. The repo table is optional and is the complete
authoritative gate set; omitting it configures zero repository gates. For example, a
user file might supply several roles under `[tiers]`:

```toml
# ~/.director/config.toml
[tiers]
planner  = "claude-code/opus"
executor = "opencode/openrouter/deepseek/deepseek-v4-pro"
reviewer = "claude-code/sonnet"
```

And a repo file overrides just one role:

```toml
# <repo>/.director/config.toml
[tiers]
executor = "opencode/lmstudio/qwen3.6-27b-mtp"
```

The resulting effective config uses the repo-local `executor` but falls back to the user
values for `planner` and `reviewer` (each role binds to a single `"provider/model-ref"`
string — the repo `[tiers]` overrides only the roles it names).

### `director init` target selection

`director init` auto-detects its write target: inside a git repo it writes `<repo>/.director/config.toml`; outside any git repo it writes `~/.director/config.toml`. It offers optional gate configuration only for the repo-local target. You can override this behavior with flags (mutually exclusive):

- `--user` forces the user-level target (`~/.director/config.toml`).
- `--local` forces the repo-level target (`<repo>/.director/config.toml`).
- `--repo PATH` still sets the target directory explicitly.

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
