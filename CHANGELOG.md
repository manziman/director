# CHANGELOG


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
