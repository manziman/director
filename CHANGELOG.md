# CHANGELOG


## v0.5.0 (2026-06-29)

### Continuous Integration

- Pin all GitHub Actions to commit SHAs; checkout to v7
  ([#14](https://github.com/manziman/director/pull/14),
  [`09155b9`](https://github.com/manziman/director/commit/09155b9d90c3d56d1043b0aa93575c1f97a1d975))

actions/checkout@v4 targets the deprecated Node.js 20 runtime. Bump checkout to v7 (Node.js 24) and
  pin every action to a full commit SHA to prevent supply-chain tampering via mutable tags. Version
  is retained in a trailing comment for readability/Dependabot.

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

### Features

- Make on-disk layout, CLI, docs, and packaging runtime-neutral
  ([#16](https://github.com/manziman/director/pull/16),
  [`675d7b5`](https://github.com/manziman/director/commit/675d7b514c12cfef4809f73fca1f1ecf6f1126bb))

OpenCode's name was baked into paths, help text, package metadata, and docs, presenting it as THE
  runtime rather than one peer. A Claude-Code-only user still got an .opencode/ tree and read docs
  framing OpenCode as a hard prerequisite.

- setup.sync_agents(repo, cfg=None) is now config-aware: it renders .opencode/ agents + starter
  opencode.json ONLY when an OpenCode-owned provider is selected (detected via #9's
  OPENCODE_PROVIDERS / registry). A claude-code-only project gets no .opencode/ tree; a
  malformed/absent config writes nothing under .opencode/. .director/.gitignore is still always
  written, and an existing .opencode/ tree is never deleted or clobbered (opencode.json stays
  only-if-absent). - cli.py and plan.py pass their loaded Config to sync_agents so gating is
  precise; sync-agents help and its output message are runtime-neutral. - init.py's
  unavailable-models warning is provider-neutral. - pyproject description + keywords and both
  READMEs present OpenCode and Claude Code as interchangeable peers, list each runtime as a
  conditional prerequisite, and reorder the quickstart (init before sync-agents). Accurate
  runtime-specific docs (e.g. OpenCode NDJSON details) are kept, not over-neutralized.

Full suite green (356 tests); ruff clean. Stdlib only.

Closes #10. Part of #8.

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

- Pluggable per-runtime model discovery and provider-neutral pricing
  ([#17](https://github.com/manziman/director/pull/17),
  [`eb6e338`](https://github.com/manziman/director/commit/eb6e338fa7e5a8ed83c8acb6dac91280649afa6a))

#9 already made provider resolution unbiased (registry, no default); this adds the remaining
  model-agnosticism pieces from #11.

- The Runtime protocol gains discover_models() -> list of "<provider>/<model>" tier strings: an
  additive, init-time-only hook that never raises and returns [] when a runtime's source is
  unavailable. It is independent of resolution — run_agent / runtime_for_model / OPENCODE_PROVIDERS
  are unchanged and stay offline-safe for claude-code-only users. - OpenCodeRuntime.discover_models
  shells out to `opencode models`. - ClaudeCodeRuntime.discover_models returns a curated
  claude-code/{opus, sonnet,haiku} list (the claude CLI has no model-listing command). -
  runtime.runtimes() returns the unique registered runtimes in registration order. `director init`
  now enumerates models by unioning every runtime's discover_models() (deduped, stable order)
  instead of calling `opencode models` directly, with the free-text fallback intact — so a
  claude-code-only user is offered claude-code/* tiers. - config.example.toml adds claude-code/*
  pricing examples and documents that any unlisted model is priced at $0.

Full suite green (469 tests); ruff clean. Stdlib only.

Closes #11. Part of #8.

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

- Provider-runtime registry replacing the OpenCode-default prefix router
  ([#15](https://github.com/manziman/director/pull/15),
  [`a628d5d`](https://github.com/manziman/director/commit/a628d5dfa02611316350192b7f4597818c11bf91))

Replace the hardcoded 2-way string-prefix runtime router (OpenCode as the implicit `else`) with an
  explicit provider->runtime registry behind a small Runtime protocol.

- New director/runtime.py holds the dependency-free primitives: the Runtime protocol, RunResult, and
  the registry (register/resolve/runtime_for_model). This also breaks the opencode<->claudecode
  import cycle. - Each runtime declares the provider segments it owns. ClaudeCodeRuntime owns
  {"claude-code"}; OpenCodeRuntime owns a curated OPENCODE_PROVIDERS allowlist. There is no
  catch-all default: an unrecognized provider yields an error RunResult naming the bad provider and
  the registered runtimes (run_agent still never raises). - run_agent now resolves the runtime via
  the registry. Its public keyword signature and the director.opencode import surface (RunResult,
  watch_it_fail, _run_opencode, run_agent) are unchanged, so existing callers and test monkeypatch
  points keep working. - Adding a third runtime (e.g. #6, Codex) is now a register() call, not a new
  elif + bespoke parser.

Routing is preserved: claude-code/<m> -> claude --model <m> (first segment stripped, remaining
  slashes kept); anthropic/, lmstudio/, amazon-bedrock/, openrouter/, ... -> OpenCode unchanged.

Closes #9. Part of #8.

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>


## v0.4.1 (2026-06-25)

### Bug Fixes

- Prune stale director-authored test files on plan re-run
  ([#5](https://github.com/manziman/director/pull/5),
  [`e644239`](https://github.com/manziman/director/commit/e644239974840cfe3518138089f0872f3d02c976))

`_author_tests` overwrote the current DAG's test files but never removed test files authored by an
  earlier attempt of the same job whose nodes are gone from the current DAG. `commit_all`'s `git add
  -A` then swept those stale files onto the job branch, and a later run's full-suite integration
  gate failed on the obsolete contracts.

Prune `prev_tests - current_tests` before committing, where prev_tests is the union of the previous
  plan.json's node.tests (read from disk in _stage_bc_decompose before it is overwritten; taken from
  the in-memory pre-revision plan in _critique_plan). The prune set is always a subset of a prior
  DAG's node.tests, so hand-written suite files are never deleted.

Authored by director dogfooding itself (planner+test-author: opus, executor: sonnet, via the
  claude-code provider).

Closes #2

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>


## v0.4.0 (2026-06-25)

### Continuous Integration

- Install build in semantic-release build_command (container lacks it)
  ([`0bdd056`](https://github.com/manziman/director/commit/0bdd05666f86a93ec37cd7738075eea7ac50143a))

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

- Make releases manual-only (workflow_dispatch, not every push to main)
  ([`bddcb57`](https://github.com/manziman/director/commit/bddcb5753a66f11059d47e78a4b54168d5f1b4e0))

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

- Push releases to protected main via a RELEASE_TOKEN PAT
  ([#4](https://github.com/manziman/director/pull/4),
  [`776db5b`](https://github.com/manziman/director/commit/776db5b3e2ab23acedd820ebba47b538a823cacf))

main requires PRs, so the default GITHUB_TOKEN can't push semantic-release's version commit + tag.
  Use a RELEASE_TOKEN secret (a PAT with Contents:write whose owner is on the main ruleset bypass
  list) for checkout + the semantic-release action. Falls back to GITHUB_TOKEN when unset. PyPI
  publishing is unaffected (OIDC).

### Features

- Add Claude Code agent-runtime provider
  ([`71b0819`](https://github.com/manziman/director/commit/71b081998969ec9e0db7f16884eba015cf9fad2f))

Per-role runtime via the tier model-string prefix (claude-code/<model> → claude CLI; anything else →
  opencode). Includes the live-caught dispatch fix and the prefix-selection refactor from PR review.
  Tests stub the subprocess — no model/network in CI.

- Add interactive `director init` command
  ([`8e8ba7d`](https://github.com/manziman/director/commit/8e8ba7da4489ee7da10b70b8d755fb3eb861e440))

`director init` configures .director/config.toml by asking questions instead of copying a static
  example: it discovers models via `opencode models`, lets the user pick one per role, prompts for
  the test/lint/typecheck gate commands, and writes a minimal config (with a pointer to
  config.example.toml for advanced sections). `sync-agents` no longer seeds the config; the "missing
  config" error now points at `director init`.

Built end-to-end with director dogfooding itself (planner: Opus 4.8, executor: local Qwen3.6-27B):
  5/5 nodes at the executor tier first attempt, integration gate green.


## v0.3.0 (2026-06-24)

### Documentation

- Move design lessons inline; drop the standalone lessons doc
  ([`134c202`](https://github.com/manziman/director/commit/134c202e9e1df98bc20d1f7a51ab7619dd2a55fe))

The cross-phase lessons now live as comments at their point in the code (gitignore handling in
  setup/gates/opencode, deterministic-gates in gates/review/opencode, config-as-object in
  config/bench, non-fatal cleanup in bench, terminal-state scheduling in run). CONTRIBUTING points
  to those locations; the two process notes (offline-stubs-vs-live, wall-clock cutoffs) live in the
  tests docstring and CONTRIBUTING. The standalone docs/lessons-learned.md is removed from the repo.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
