# Contributing to director

Thanks for your interest! director is a small, dependency-free Python CLI. The bar is
"readable, tested, and idiomatic."

## Development setup

```bash
uv sync                                            # dev environment (ruff, semantic-release, build)
uv run python -m unittest discover -s tests -q     # run the test suite
uvx ruff check director tests                      # lint
uvx ruff format director tests                     # format
uv build                                           # build sdist + wheel
```

The test suite stubs the single external boundary (`opencode.run_agent`) and drives real
temporary git repos, so it needs **neither OpenCode nor any model provider** — it runs
fully offline and deterministically in CI across Python 3.11–3.14.

## Conventions

- **Standard library only.** No runtime dependencies. If you reach for a third-party
  package, there is almost certainly a stdlib way that fits this project's constraints.
- **Roles, never models.** Code, prompts, and logs refer to roles (`planner`, `executor`,
  `reviewer`, …) — never to a specific model or to "local"/"cloud". Model strings live in
  `.director/config.toml` only.
- **Deterministic gates decide merges.** Exit codes (tests/lint/typecheck), never an LLM's
  opinion. LLM review is advisory or cost-gated, layered *after* the deterministic gate.
- **`ruff` is the linter + formatter.** CI enforces both `ruff check` and `ruff format --check`.

## Commit messages → automated releases

Releases are fully automated by [python-semantic-release](https://python-semantic-release.readthedocs.io/)
on merge to `main`. Use [Conventional Commits](https://www.conventionalcommits.org/):

- `fix: …` → patch release
- `feat: …` → minor release
- `feat!: …` or a `BREAKING CHANGE:` footer → major release
- `docs: …`, `test: …`, `chore: …`, `refactor: … `→ no release on their own

On merge, semantic-release computes the next version, updates `CHANGELOG.md` and
`director/__init__.py`, tags, creates the GitHub release, and publishes to PyPI via
Trusted Publishing.

## Lessons that shaped the design

Several non-obvious decisions were paid for with real bugs; each is documented at
its point in the code — read the comment before changing that logic:

- **Respect the target repo's `.gitignore`.** Tooling writes untracked files that
  `git add -A` then sweeps into commits, breaking later merges/checkouts. See
  `setup.ensure_director_gitignore`, `gates._is_ignorable`, and `opencode._CLEAN_ENV`.
- **Deterministic gates decide; LLM judgment is advisory or cost-gated.** See the
  `gates` module docstring, `review.review_node`, and `opencode.watch_it_fail`.
- **Pass config as an object; never re-read it mid-operation.** See `config.load_file`
  and the `bench` module docstring.
- **Cleanup is non-fatal once the result is collected.** See the `finally` block in
  `bench.run_bench`.
- **The scheduler tracks terminal state** (done|failed|escalated) so a failed node is
  never re-scheduled. See `run.run_job`.

Two process notes with no single code home: offline stubs prove control flow but only
live runs find integration bugs (see the `tests/` module docstring), and live runs
should always be wrapped in a wall-clock `timeout` to bound a logic-bug runaway.
