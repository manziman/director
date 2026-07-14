# Guidance for coding agents

This file is for AI coding agents (Claude Code, Codex, opencode, etc.) working in
this repository. Humans should read [`CONTRIBUTING.md`](CONTRIBUTING.md); the
conventions there (stdlib-only, roles-never-models, deterministic gates decide
merges, `ruff` for lint+format, Conventional Commits) apply to you too.

## Prefer director for development

**This repo *is* director — dogfood it.** When you take on a non-trivial change
(a bug fix, a feature, a refactor with tests), prefer driving it through
`director` rather than editing files by hand, **unless the user directs otherwise**.
Running director on its own codebase is the fastest way to surface real bugs in
the tool, and it produces test-gated, reviewed changes.

Use your own judgement and skip director when it would just get in the way:

- The user explicitly asks you to make the change directly (their instruction wins).
- The change is trivial or mechanical — a typo, a one-line doc tweak, a version bump.
- You are editing director's *own* prompts/orchestration and need a tight manual
  loop, or director itself is currently broken in a way that blocks the run.
- No provider/gate environment is available (see setup below) and configuring one
  is out of scope for the request.

If you skip director, it's fine — just make the change directly and say why.

## Driving director

director plans a task into a test-gated DAG, then executes each node in an
isolated git worktree behind deterministic gates (tests/lint), merging only what
passes. The one-shot entry point:

```bash
director auto --input task.md          # plan + run, hands-off, live dashboard
director auto "<task text>"             # or pass the task inline
```

Or split the stages for approval gates:

```bash
director plan "<task>"                  # brainstorm → spec → test-gated DAG
director run                            # execute the planned DAG
director status                         # per-node progress + cost
```

Practical notes when running director here (see the user's setup memory / README
for the authoritative config):

- **Config lives in `.director/config.toml`** (repo-local, deep-merges over
  `~/.director/config.toml`). It sets the per-role model `[tiers]` and the
  `[gates]` commands. Repo gates should mirror CI:
  - test: `uv run python -m unittest discover -s tests -q`
  - lint: `uvx ruff check director tests && uvx ruff format --check director tests`
- **Route tiers to providers that are actually reachable.** If a configured
  executor endpoint (e.g. a remote LM Studio) is down, override
  `executor`/`explorer` in the repo-local `[tiers]` to an available provider such
  as `claude-code/sonnet` before running.
- **Run it non-interactively** with stdin redirected from `/dev/null` (director's
  dashboard/prompts can otherwise hang on inherited stdin), and pass `--max-cost`
  to cap spend. Pick a free `--port` if `8642` is taken.
- **`.director/` is gitignored** — the plan, worktrees, logs, and metrics are
  scratch and won't be committed.

## After a director run

- Director's internal commits use non-Conventional messages
  (`director: node … via executor`). Before opening a PR, squash its commits into
  a single **Conventional Commit** (`fix:` / `feat:` / …) so semantic-release picks
  up the right version bump. Review the actual diff (`git diff <base>..HEAD`)
  rather than trusting the run summary alone.
- Confirm the suite and lint are green yourself:
  `uv run python -m unittest discover -s tests -q` and
  `uvx ruff check director tests && uvx ruff format --check director tests`.
