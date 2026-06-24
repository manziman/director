# Changelog

All notable changes to this project are documented here. This file is maintained
automatically by [python-semantic-release](https://python-semantic-release.readthedocs.io/)
from [Conventional Commits](https://www.conventionalcommits.org/); the entry below is the
pre-automation baseline.

<!-- version list -->

## v0.3.0 (2026-06-24)

Initial public baseline (project renamed from its `foreman` codename to **director**).

### Features

- **plan / run / status orchestrator** over OpenCode: a strong planner decomposes a task
  into an atomic DAG with acceptance tests written first; a cheaper executor implements
  each node in an isolated git worktree; deterministic gates (tests/lint/typecheck) decide
  merges, with a per-task escalation ladder.
- **Approval gates + methodology** (brainstorm/spec gate, plan gate; `--auto` self-critique),
  two-stage cost-gated code review, and red-green test-hash hardening.
- **TDD hardening & measurement:** watch-it-fail transcript verification, flake control
  (re-run node tests on success), a `.director/metrics.jsonl` stream, and `director bench`
  to compare cost/quality/wall-time across profiles on identical acceptance tests.
- Three shipped profiles: `local-first`, `cheap-cloud`, `all-frontier`.
