"""Thin git helpers. Director uses real git branches + worktrees for isolation."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(args: list[str], cwd: str | Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def current_commit(cwd: str | Path) -> str:
    return git(["rev-parse", "HEAD"], cwd).stdout.strip()


def current_branch(cwd: str | Path) -> str:
    return git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()


def branch_exists(name: str, cwd: str | Path) -> bool:
    return git(["rev-parse", "--verify", "--quiet", name], cwd, check=False).returncode == 0


def create_branch(name: str, cwd: str | Path, base: str | None = None) -> None:
    args = ["branch", name] + ([base] if base else [])
    git(args, cwd)


def checkout(name: str, cwd: str | Path) -> None:
    git(["checkout", name], cwd)


def worktree_add(path: str | Path, branch: str, base: str, cwd: str | Path) -> None:
    """Create a new branch `branch` from `base` checked out at `path`."""
    git(["worktree", "add", "-b", branch, str(path), base], cwd)


def worktree_remove(path: str | Path, cwd: str | Path) -> None:
    git(["worktree", "remove", "--force", str(path)], cwd, check=False)


def changed_paths(cwd: str | Path) -> list[str]:
    """All paths modified/added/deleted vs HEAD, including untracked (ignored
    files excluded). Used to enforce the file allowlist."""
    out = git(["status", "--porcelain", "--untracked-files=all"], cwd).stdout
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # format: "XY <path>" or rename "XY old -> new"
        p = line[3:]
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        paths.append(p.strip().strip('"'))
    return paths


def commit_all(message: str, cwd: str | Path) -> bool:
    """Stage everything and commit. Returns False if there was nothing to commit.
    Signing is disabled so headless runs never block on a passphrase."""
    git(["add", "-A"], cwd)
    if not git(["status", "--porcelain"], cwd).stdout.strip():
        return False
    git(["-c", "commit.gpgsign=false", "commit", "--no-verify", "-q", "-m", message], cwd)
    return True


def merge_branch(
    branch: str, cwd: str | Path, message: str | None = None
) -> subprocess.CompletedProcess:
    """Merge `branch` into the current branch (no fast-forward, unsigned)."""
    args = ["-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-verify"]
    if message:
        args += ["-m", message]
    args.append(branch)
    return git(args, cwd, check=False)
