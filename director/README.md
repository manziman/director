# `director` — the orchestrator (Phase 2 + 2.5 + 3)

A thin CLI that drives OpenCode headlessly to run the decomposition harness.
Stdlib-only (Python ≥ 3.11). The harness consumes configured OpenAI-compatible
endpoints; it never manages providers.

```
director plan "<task>" [--repo .]             # interactive: stops at each approval gate
director plan --continue                      # resume after editing/approving the gate artifact
director plan "<task>" --auto                 # planner self-critiques at each gate; no pause
director plan "<task>" --auto --no-critique   # gates auto-pass, fully hands-off
director run [--repo .] [--parallel N] [--max-attempts K]
director status [--repo .]
director bench "<task>" --profiles all-frontier,cheap-cloud,local-first [--plan-profile P]
director sync-agents [--repo .]               # (re)install role agents into <repo>/.opencode
```

## Flow

**plan** — a re-entrant pipeline with two artifact-based approval gates (Phase 2.5).
A job branch `director/job-<id>` is created and the role agents synced onto it first.
1. `explorer` (cheap tier) does read-only recon → `.director/recon.md`.
2. **Stage A — brainstorm/spec.** `brainstorm` (planner tier) does a Socratic
   refinement pass and writes a readable design spec → `.director/spec.md`.
   → **Gate 1.**
3. **Stage B — decompose.** `planner` (planner tier) turns the *approved spec*
   into a strict-JSON DAG → `.director/plan.json`. Each node: `id, title, spec
   (junior-engineer standard), files (allowlist), depends_on, test_cmd, tests,
   estimated_difficulty`. Validated: acyclic, deps resolve, **concurrent nodes
   have disjoint allowlists**.
4. **Stage C — test authoring.** `test-author` (frontier tier) writes each node's
   tests, committed to the job branch; director verifies they **fail first** (red)
   and **hashes** each test file (the contract is then immutable). → **Gate 2.**

Gates are **artifact-based, not process-blocking**: director writes the artifact and
exits; the human edits/approves on disk and resumes with `--continue`. `--auto`
swaps a one-call planner **self-critique** into the same gate (re-read artifact vs.
the request, revise once); `--no-critique` makes gates auto-pass. Human and
self-critic are mechanically the same gate — only the approver differs.

**run** — for each node in dependency order (up to `--parallel` at once):
1. `git worktree add` an isolated task branch off the job branch.
2. Invoke `executor` (executor tier) with spec + allowlist file contents + the
   failing test output. (Executor mandate: **watch it fail first**.)
3. **Deterministic gate** (exit codes only): test files byte-for-byte intact (hash),
   `node.test_cmd` passes, AND the diff touches only the allowlist. On the pass
   path, **flake control** (Phase 3) re-runs the tests `flake_runs` times (default
   2); any mismatch fails the node as flaky.
4. **Two-stage review** (Phase 2.5), after the deterministic gate, before merge:
   - *Stage one — spec compliance:* the deterministic gate above, plus an optional
     advisory explorer-tier check (`review.stage_one_llm`, off by default).
   - *Stage two — code quality (`reviewer` tier):* **cost-gated** — runs only when
     the node escalated OR its diff touched > `review.stage_two_file_threshold`
     files (default 3). Never runs on the cheap/local tier. A `critical` finding
     blocks the merge and **re-opens the node** (counts against `max_attempts`).
5. Fail/blocked → feed the gate or review output back, retry up to `max_attempts`
   (fresh OpenCode context each attempt). Exhausted → retry the SAME node once at
   the `escalation` tier (never the whole job).
6. Pass → commit + merge into the job branch; mark done in `.director/state.json`.
   After all nodes: an **integration gate** runs the repo-wide suite/lint/typecheck.

Each node's transcript is also checked for **watch-it-fail** (Phase 3 §1): did the
executor run the failing tests *before* its first edit? This is advisory (the
deterministic gate already enforces the contract) and recorded as a metric —
`observed` / `not_observed` / `unknown`.

**status** — per-node state, attempts, cost, executor-tier completion rate (the
falsifiable hypothesis target: >70% of nodes done without escalation), stage-two
review trigger rate, and watch-it-fail observed count.

## Measurement (Phase 3)

Every `run` appends to **`.director/metrics.jsonl`** — one `kind:"node"` record per
node (tier/model, attempts, escalation, per-role tokens+cost, wall time,
watch-it-fail verdict, flake outcome) and one `kind:"run"` summary (the derived
rates: executor-tier completion, escalation, stage-two trigger, total wall time
and cost, plus the resolved tier map). This is the falsifiability instrument; it
is what `director bench` reads.

**bench** — the experiment. Plans the task **once** (under `--plan-profile`,
default `all-frontier`) so the DAG and acceptance tests are frozen, then runs that
*same* plan under each `--profiles` profile by forking a fresh job branch off the
frozen one (every profile faces byte-for-byte identical tests). It diffs cost /
quality (same acceptance tests) / wall-time and reports each profile's run-cost
reduction vs the `all-frontier` baseline (target: >80%). The active `config.toml`
is never touched — each profile's config is loaded directly from its profile TOML.
Per-profile metrics streams and a `summary.json` land in `.director/bench/`.

## Roles → tiers

Roles bind to `provider/model` strings in `.director/config.toml` (`[tiers]`).
Code/logs name only roles. `director` passes the resolved model via `opencode run
--agent <role> --model <tier>`, so **switching executor models is a config edit,
never a code change.** `sync-agents` seeds `.director/config.toml` from the bundled
`config.example.toml`; edit it to bind roles to models. For `bench`, create
`.director/profiles/<name>.toml` variants (copy `config.toml`, change the executor tier).

## Deliberate deviations from the spec

- **Tests live on the job branch**, not a separate `director/tests-<id>` branch
  (dependent nodes need both the tests and prior nodes' impls; one branch is
  simpler and equivalent).
- **The full repo-wide test suite is the *integration* gate, not a per-node gate.**
  Sibling nodes' tests are intentionally red until their own node runs, so a
  per-node full-suite gate would always fail mid-DAG. Per node we gate on
  `node.test_cmd` + allowlist; the full suite/lint/typecheck run once after merge.

## Persistence (`.director/`, all resumable/debuggable)

- `spec.md` — approved design spec (Gate 1).  `recon.md` — explorer summary.
- `plan_stage.json` — which gate the plan is paused at (drives `--continue`).
- `plan.json` — the DAG (incl. per-node `test_hashes`).  `state.json` — per-node
  status/attempts/cost + review trigger info (resume).
- `costs.jsonl` — every model call tagged with role + resolved model (local = $0).
- `metrics.jsonl` — per-node + per-run measurement stream (Phase 3).
- `bench/` — `summary.json` + per-profile `*.metrics.jsonl` from `director bench`.
- `logs/*.jsonl` — raw OpenCode NDJSON events per call (`.stderr` siblings = logs).
- `worktrees/` — transient per-node worktrees.

## Limits (config `[limits]`)

`node_timeout_secs` (per call), `cost_ceiling_usd` (abort the run when exceeded;
local = $0 so local-first never trips it), `max_attempts`, `flake_runs` (Phase 3
flake control: times to run a node's tests on success; default 2, 1 disables).
