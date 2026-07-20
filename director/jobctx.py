"""JobContext — where a job reads its checkout and writes its artifacts.

Historically every path was derived from `<repo>/.director`: planning artifacts,
run state, costs, metrics, logs, and the per-node worktree root all lived under
the repository the user invoked director in. That coupling blocks running
several jobs against the same repository at once (issue #38): each job needs its
own checkout and its own artifact tree, outside the submitted repo.

A JobContext names the split explicitly:

- ``source_repo``   — the repository the job targets (never mutated by agent jobs
  beyond branch/worktree creation).
- ``workspace``     — the checkout planning/execution operate in. Legacy commands
  work directly in ``source_repo``; agent jobs get an isolated top-level
  worktree on a unique job branch.
- ``artifact_dir``  — where generated state lands (plan_stage.json, plan.json,
  state.json, costs.jsonl, metrics.jsonl, recon.md, spec.md, logs/).
- ``node_worktree_dir`` — root for per-node worktrees; ``None`` keeps the legacy
  ``$TMPDIR/director-worktrees/<job_id>`` location.

``job_id``/``job_branch``/``base_commit`` are pre-assigned for agent jobs (the
supervisor captures them at submission); legacy planning derives them itself.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JobContext:
    source_repo: Path
    workspace: Path
    artifact_dir: Path
    node_worktree_dir: Path | None = None
    job_id: str | None = None
    job_branch: str | None = None
    base_commit: str | None = None

    @classmethod
    def for_repo(cls, repo: str | Path) -> JobContext:
        """The legacy layout: work in the repo, artifacts under `<repo>/.director`."""
        repo = Path(repo).resolve()
        return cls(source_repo=repo, workspace=repo, artifact_dir=repo / ".director")

    @property
    def logs_dir(self) -> Path:
        return self.artifact_dir / "logs"

    def worktree_root(self, job_id: str) -> Path:
        """Per-node worktree root for `job_id` (worktrees must live outside the
        workspace tree so an agent can't resolve the enclosing repo as its
        project root — see run.py)."""
        if self.node_worktree_dir is not None:
            return self.node_worktree_dir
        return Path(tempfile.gettempdir()) / "director-worktrees" / job_id
