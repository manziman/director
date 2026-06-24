# Lessons learned

Design lessons from building director, each paid for with a real bug or a real
measurement — the durable distillation for anyone changing the gate, scheduler, or
git logic.

## 1. Offline stubs prove control flow; only live runs find integration bugs

Every phase validated the *new control flow* offline by stubbing the single
external boundary (`opencode.run_agent`) against real temp git repos. That caught
logic errors fast and cheaply — but **every integration-level bug escaped the
stubs and only surfaced live**, against real OpenCode + real git + a real model:

- Phase 2.5: a scheduler that re-ran failed nodes forever; spec-critique prose
  bleeding into the spec; `__pycache__` tripping the allowlist gate; `.pyc` files
  committed by `git add -A` breaking later merges; stale worktree registrations.
- Phase 3: `.director/` runtime files swept into the job branch by `git add -A`,
  which then blocked `git checkout` and crashed `bench`'s cleanup.

**Takeaway:** keep the offline suite (it's the fast feedback loop and the CI
gate), but treat a live run as mandatory acceptance for anything touching git,
the process environment, or the filesystem. Stubs can't model those.

## 2. Respect the target repo's `.gitignore` — the single most recurring root cause

The same class of bug bit twice: **tooling writes untracked files into the work
tree, `git add -A` sweeps them into commits, and they then break merges /
checkouts / file-count heuristics.** Python `.pyc` in Phase 2.5; director's own
`.director/` runtime files in Phase 3.

director's repo gitignores these, so its own dogfooding never hit it — but an
arbitrary target repo does. The durable, language-neutral fixes:

- `setup.ensure_director_gitignore()` seeds `.director/.gitignore` so director's
  own artifacts are never committable (config + profiles stay tracked).
- `PYTHONDONTWRITEBYTECODE=1` on every spawned process so bytecode is never
  created in the first place.
- The gate's `_is_ignorable()` filter as a backstop for ephemeral build noise.

The general principle for any future ecosystem (JVM `target/`, `node_modules`,
coverage files): **never assume the work tree is clean of tool output — either
suppress it at the source or make `git` ignore it; don't filter it after the
fact.**

## 3. Always wrap local/live runs in a wall-clock cutoff

A scheduler bug in Phase 2.5 re-scheduled a failing node forever, including cloud
escalation each time — a true runaway. Every live `director`/`bench` invocation
since is wrapped in a realistic `timeout`. Cheap insurance against a logic bug
turning into an unbounded spend or an overnight GPU burn.

## 4. Deterministic gates decide; LLM judgment is advisory or cost-gated

Merge decisions are exit codes — tests, lint, typecheck, allowlist, test-hash —
never a model's opinion. Where an LLM *is* useful (stage-two code review,
watch-it-fail verification), it is layered *after* the deterministic gate and is
either cost-gated (only fires when escalated or the diff is large) or purely
advisory (recorded as a metric, never blocking). Heuristics over model output —
e.g. parsing a transcript for "ran tests before editing" — vary too much across
the 75+ providers OpenCode abstracts to be a hard gate.

## 5. Pass config as an object; never re-read it from disk mid-operation

`run_plan`/`run_job` take a resolved `Config` object and never re-read
`config.toml`. This is what let `bench` run several profiles in one invocation
without swapping the tracked `config.toml` on disk — the first design *did* swap
it, which dirtied a tracked file and made `git checkout` refuse. `config.load_file()`
loads each profile directly into an object. **Keep on-disk config as an input you
read once, not shared mutable state.**

## 6. Make cleanup non-fatal once the result is collected

`bench` writes `summary.json` *before* its `finally`-block branch restore. When
that restore later failed (lesson #2), the whole command crashed and the report
never printed — even though all the data was already on disk. Cleanup that runs
after the valuable work is done should log and continue, not raise over a result
the caller has already earned.
