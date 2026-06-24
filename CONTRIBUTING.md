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

Before changing the gate/scheduler/git logic, skim [`docs/lessons-learned.md`](docs/lessons-learned.md)
— several non-obvious decisions there were paid for with real bugs.
