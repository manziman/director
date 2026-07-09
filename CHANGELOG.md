# CHANGELOG


## v0.10.1 (2026-07-09)

### Bug Fixes

- Merge inherited config in sync-agents ([#34](https://github.com/manziman/director/pull/34),
  [`83260ff`](https://github.com/manziman/director/commit/83260fff77429e49384ab160556ef53fa0efdc73))


## v0.10.0 (2026-07-08)

### Features

- Add director auto — one-shot autonomous mode with live dashboard (#30)
  ([#31](https://github.com/manziman/director/pull/31),
  [`ea7c045`](https://github.com/manziman/director/commit/ea7c045c4c1f532b06d3c7b3f3506722a60a1885))

`director auto` fuses plan + run behind a single unattended command and serves the read-only web
  dashboard from the same process for the duration.

- Task input: positional string, --input FILE, or --input - (stdin drained to EOF before anything
  spawns). Explicit-but-empty input errors. - Pipeline: run_plan(auto=True, critique per
  --no-critique) -> READY -> run_job -> run_summary, with cmd_run's exit-code semantics. - Dashboard
  binds BEFORE planning (fail fast on a busy port) and serves from a daemon thread; --open implies
  --hold, and --hold keeps the dashboard live inside the server's try/finally until Ctrl-C. - Resume
  guard: a supplied task differing from plan_stage.json's recorded task errors (both named,
  truncated to 80 chars); --force validates the new task first, then discards the five planning
  artifacts and replans; READY + plan.json + matching/absent task skips planning entirely. -
  --max-cost aborts at the phase boundary via CostLedger.total(). - Hardening: run_shell/popen_tree
  now default stdin=subprocess.DEVNULL (caller kwargs still win). All provider adapters spawn via
  popen_tree, fixing the OpenCode inherited-stdin hang (exit 124 at node_timeout) at the source —
  `director … </dev/null` is no longer needed.

Implemented by director dogfooding its own pipeline: 2 nodes planned by Opus, executed at executor
  tier by a local Qwen3.6 27B (100% executor-tier completion, 0 escalations, $4.68 total, all
  planning), gates reviewed by a human at both artifacts, plus a human polish commit (lint, --hold
  serving liveness, --force delete-after-validate) with regression tests.

tests: 47 new (test_cli_auto.py, test_proc_stdin.py); suite at 873 green.

Closes #30

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

- Add director ui — live web dashboard for run/DAG observability (#18)
  ([#29](https://github.com/manziman/director/pull/29),
  [`f9a1c80`](https://github.com/manziman/director/commit/f9a1c80e8a2b5c48a40b0cd9854a78369db4ad94))

`director ui` serves a localhost, read-only dashboard assembled entirely from what any runtime
  already writes under <repo>/.director/ — no new instrumentation, no Python dependencies, no build
  step, no vendored JS.

- director/web/server.py: stdlib ThreadingHTTPServer with GET /, /api/state (merged plan + state +
  summary + cost + latest run metrics), /api/logs/<node> (plan-id allowlisted, traversal-rejected,
  tail-capped), /api/artifact/<recon|spec> (fixed allowlist), /api/health. Pure
  build_state/read_node_logs/read_artifact functions keep the HTTP layer thin and unit-testable.
  Malformed mid-write reads serve the last good snapshot. - director/web/index.html: single
  self-contained page. Hand-rolled layered SVG DAG (layers = longest path from a root via
  dag.topo_order), status colors/glyphs matching report._STATUS_GLYPH, 1.5 s polling that only
  re-renders on change, per-node detail + log viewer, cost/metrics tables, and a planning-pipeline
  stepper (recon → spec → gate 1 → decompose → gate 2 → ready) with recon/spec artifact viewers
  driven by plan_stage.json — so the dashboard is live from `director plan` onward. -
  report.status_summary(): shared aggregate math so status_table and the UI can't drift. -
  RunState.save() now writes atomically (temp + os.replace), removing the partial-read race at the
  source; .director/.gitignore gains *.tmp. - Packaging: director/web/* force-included via hatch
  artifacts (the 0.8.1 gitignore lesson); verified present in a built wheel.

Read-only by design: the UI observes .director/, never mutates it; gates are still approved via
  `director plan --continue`. Localhost bind by default, with a warning when bound elsewhere.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>


## v0.9.0 (2026-07-03)

### Features

- Add Codex provider (#6) + fix 0.8.1 wheel packaging
  ([#27](https://github.com/manziman/director/pull/27),
  [`01bbc70`](https://github.com/manziman/director/commit/01bbc706ed73f0f4f7f4eea84be4a17fecf4ec41))

* fix: ship agent_templates package data despite json ignore pattern

The blanket `*.json` gitignore added in #26 also matched director/agent_templates/opencode.json —
  hatchling honors VCS ignore patterns when selecting build contents, so the 0.8.1 wheel shipped
  without the template and `director plan`/`sync-agents` crashed with FileNotFoundError at first
  use.

Fix on both layers: - .gitignore: negate the pattern for director/agent_templates/*.json -
  pyproject: [tool.hatch.build] artifacts force-includes the templates regardless of ignore rules

tests/test_packaging.py pins the invariant (git check-ignore matches no tracked template; artifacts
  covers the templates dir).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

* feat: add Codex agent provider (#6)

Squash of the director job 20260701-155321 node commits (acceptance tests + codex-module,
  dispatch-registration-wiring, config-validation-wiring, docs-config-example, docs-readme):

- director/codex.py: run_codex + _parse_codex + self-registering CodexProvider (name "codex"),
  mirroring director/claudecode.py - registration wiring in opencode.py, init.py, and
  config._ensure_builtin_providers_registered so codex/... tiers and pricing keys validate -
  tier-prefix docs in config.example.toml and README - tests: test_codex.py, test_codex_dispatch.py,
  plus dispatch/config validation coverage

* fix: align codex provider with the real codex exec --json contract

The generated provider was written against an assumed output format and a bare `codex exec`
  invocation. Verified against codex-cli 0.139:

- run_codex now passes --json (structured JSONL; without it stdout is human text and the parser
  degrades to raw-text with no telemetry), --dangerously-bypass-approvals-and-sandbox (codex
  sandboxes by default, which blocks file edits; mirrors claude's --dangerously-skip-permissions —
  director already isolates runs in disposable worktrees), and --skip-git-repo-check. - _parse_codex
  now reads the real event shapes: item.completed (agent_message text,
  command_execution/file_change/... tool items), turn.completed usage
  (input/cached_input/output/reasoning_output tokens), error, and turn.failed. Codex emits no cost
  events; cost_reported stays 0.0 and pricing config handles dollars. - CODEX_MODELS lists verified
  aliases (gpt-5.5; gpt-5-codex is API-key-only) and its test no longer pins exact contents. -
  test_codex.py fixtures rewritten to the real contract; command test asserts --json and the
  sandbox-bypass flag.

Integration-gate fixes from the director run:

- Every test module that reloads director.provider now also reloads director.codex (and test_codex
  gained the same defensive setUpModule its siblings have) — otherwise stale _CLEAN_ENV/RunResult
  bindings break cross-module identity assertions in full-suite runs. - SIM108 ternary in run_codex;
  ruff format across the new files.

Verified end-to-end: run_agent(model="codex/gpt-5.5") against the real CLI writes files, returns
  parsed text/tokens/tool_calls, rc 0.

---------

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

### Refactoring

- Remove unused local provider config ([#28](https://github.com/manziman/director/pull/28),
  [`fb084d0`](https://github.com/manziman/director/commit/fb084d00ff57b8ce7e5a8ddc07818b6fef7f9ccd))

The [providers.local] config was loaded into Config.local but never consumed anywhere — local
  OpenAI-compatible endpoints are configured through the opencode provider (e.g.
  opencode/lmstudio/...), not through director. Drop the dead field, its loader line, the example
  TOML block, and the test coverage for it.

Closes #24 Closes #25

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>


## v0.8.1 (2026-07-01)

### Bug Fixes

- Tighten planner test_cmd guidance ([#23](https://github.com/manziman/director/pull/23),
  [`33ec442`](https://github.com/manziman/director/commit/33ec442dbd92c75bb7b04edc0ac574902ba4fb5f))


## v0.8.0 (2026-07-01)

### Features

- User-level director config overridable by repo-local config (#7)
  ([#21](https://github.com/manziman/director/pull/21),
  [`afd0f57`](https://github.com/manziman/director/commit/afd0f572183800227215f0d8b1f07f5ca58fd3a2))

* director: scaffold agents for job 20260630-140531

* director: acceptance tests for job 20260630-144342

* director: node config-two-level-merge via executor

* director: node init-target-selection via executor

* director: node cli-init-flags via executor

* director: node docs-config-example via executor

* director: node docs-readme via executor

* style: ruff --fix + format on director-authored nodes (import sort, combine with)

Finalizes the integration lint gate for job 20260630-144342 (issue #7). Auto-fixable ruff findings
  only: import ordering in cli.py/test files and a combined `with` in test_cli_init.py. No behavior
  change.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

* docs: fix README two-level [tiers] example to real flat schema

The executor-authored example used [tiers.planner]/model sub-table syntax, but director binds each
  role to a single "provider/model" string under a flat [tiers] table. The sub-table form would
  deserialize tiers values as dicts and break model_for(). Corrected both example blocks; merge
  semantics unchanged.

* chore: untrack paseo.json (worktree tooling, not part of issue #7)

An earlier director scaffold commit accidentally tracked this local paseo worktree config. Keep it
  on disk but out of the deliverable.

---------

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>


## v0.7.0 (2026-06-30)

### Features

- Os-agnostic portable shell execution and process-tree termination (#13)
  ([#20](https://github.com/manziman/director/pull/20),
  [`28f3c9c`](https://github.com/manziman/director/commit/28f3c9cd9a5ee28f635c2fba6bd8487d44c272f3))

Standardize director's command execution across POSIX and Windows, and kill the whole process tree
  on timeout.

New stdlib-only director/proc.py consolidates the three shell=True helpers (run.py, gates.py,
  plan.py) behind one portable helper: - _shell_argv builds an explicit [shell, -c, cmd] run with
  shell=False; POSIX uses /bin/sh, Windows prefers a POSIX shell on PATH (sh, then bash) and falls
  back to cmd /c. - run_shell captures one merged stream decoded UTF-8 with errors=replace.

Process-tree termination on timeout: - popen_tree starts each child in its own group/session
  (start_new_session / CREATE_NEW_PROCESS_GROUP); kill_tree kills the whole tree (killpg+SIGKILL /
  taskkill /F /T). - opencode.py and claudecode.py runtime-CLI launches use popen_tree/kill_tree. -
  run_shell also routes its timeout through popen_tree + kill_tree, so gate/test commands that spawn
  their own subprocess trees no longer orphan grandchildren.

CI gains an OS axis: os: [ubuntu-latest, windows-latest].

Scoped out (already correct): pathlib, tempfile.gettempdir(), splitlines(), gates.py backslash
  normalization.

Planned and implemented by director itself (dogfooding): Opus 4.8 planner and test-author,
  qwen3.6-27b-mtp executor, Sonnet 4.6 reviewer. 666 tests pass and ruff is clean.

Closes #13.

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>


## v0.6.0 (2026-06-29)

### Features

- Language/toolchain-agnostic byproduct filtering and optional target stack
  ([#19](https://github.com/manziman/director/pull/19),
  [`201c284`](https://github.com/manziman/director/commit/201c284218be349c0bf69385c566ef10f111f4d4))

director hardcoded Python toolchain assumptions in the allowlist gate, the subprocess env, the
  watch-it-fail analyzer, prompt templates, and config examples. This makes them language-neutral
  and lets the target stack be declared.

- gates.py derives ignorable build byproducts generically: the union of a small DEFAULT_IGNORE
  safety net, a new [gates].ignore config key, and the TARGET repo's own .gitignore (pragmatic
  fnmatch matcher; negation lines skipped so it can only ever widen what is ignorable). A non-Python
  target's byproducts (node_modules/, target/, *.class, …) are now filtered while genuine
  out-of-scope edits are still rejected (every source is a byproduct allowlist). -
  PYTHONDONTWRITEBYTECODE is dropped from the subprocess env in runtime.py, gates.py, run.py, and
  plan.py; the _CLEAN_ENV symbol is kept (redefined as a plain env copy) to preserve the import
  surface. The generic byproduct filter now handles the .pyc problem that env var papered over, so
  this repo's .gitignore gains .pytest_cache/.mypy_cache/.ruff_cache to keep director's own dogfood
  gates clean under the new mechanism. - Config gains an optional [target] table
  (language/test_framework/toolchain), exposed as
  target_language/target_test_framework/target_toolchain properties; backward-compatible (absent ->
  {}). Declaration-first: recon stays the primary stack signal and no code path is forced to consume
  it yet. - watch_it_fail's skip-list drops the python/python3 binary names (unittest stays matched
  via _TEST_SIGNALS, preserving the Python case); the planner/test-author templates drop hardcoded
  .py example filenames while keeping the "match the repo's stack/test framework" steer;
  config.example.toml frames its gate commands as stack-specific examples.

Test fixtures cover non-Python stacks. Full suite green (566 tests); ruff clean. Stdlib only.

Closes #12. Part of #8.

Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>


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
