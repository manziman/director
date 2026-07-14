# CLAUDE.md

Guidance for Claude Code (and other coding agents) working in this repository.

See [`AGENTS.md`](AGENTS.md) for the full agent guidance and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the human contributor conventions — both
apply to you.

## The one thing to remember

**This repo is director — dogfood it.** For any non-trivial change (bug fix,
feature, tested refactor), prefer driving it through `director` rather than
hand-editing files, **unless the user directs otherwise** or the change is trivial.
See [AGENTS.md → Prefer director for development](AGENTS.md#prefer-director-for-development)
for when to use it, how to drive it (`director auto --input task.md`), and how to
turn a run into a clean Conventional-Commit PR.
