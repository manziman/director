"""Deterministic merge gates — exit codes decide, never an LLM.

Per-node gate (in the node's worktree):
  - `node.test_cmd` must pass (the node's contract), and
  - the diff must touch ONLY the node's file allowlist (rejects out-of-scope edits,
    which by construction also rejects any edit to the committed test files).

Integration gate (on the job branch, after all nodes merge):
  - every configured repository gate, in declaration order.
The full suite is NOT run per node because sibling nodes' tests are intentionally
red until their own node executes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from director import gitutil, proc
from director.config import Config
from director.models import Node

# Byproducts are now handled by the ignore matcher (build_ignore_matcher), not by
# suppressing bytecode generation. _CLEAN_ENV is kept for other env customisation.
_CLEAN_ENV = {**os.environ}
_CLEAN_ENV.pop("PYTHONDONTWRITEBYTECODE", None)


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    detail: str = ""
    summary: str = ""


@dataclass(frozen=True)
class Gate:
    """A named deterministic command, independent of configuration storage."""

    name: str
    command: str


_GATE_OUTPUT_LIMIT = 2_000
_DIAGNOSTIC_PATH = re.compile(
    r"""(?<![A-Za-z0-9_./-])
    (?P<path>
        (?:[A-Za-z]:)?(?:\.{1,2}/|/)?
        (?:[A-Za-z0-9_@+=,;.-]+/)*[A-Za-z0-9_@+=,;.-]+
    )
    (?=:\d+(?::\d+)?\b|\(\d+(?:,\d+)?\))""",
    re.VERBOSE,
)


def _head_output(output: str, limit: int = _GATE_OUTPUT_LIMIT) -> str:
    """Keep the beginning of gate output, where commands usually print context."""
    if len(output) <= limit:
        return output
    return output[:limit].rstrip() + "\n[output truncated]"


def gate_failure_detail(command: str, output: str) -> str:
    """Render command plus the useful beginning of a failing gate's output."""
    return f"$ {command}\n{_head_output(output)}"


def _diagnostic_paths(output: str, allowed: set[str]) -> list[str]:
    """Extract path-like locations from diagnostic output without tool-specific parsers."""
    paths: list[str] = []
    for match in _DIAGNOSTIC_PATH.finditer(output):
        path = match.group("path")
        normalized = path.removeprefix("./")
        # A bare word at a location can be an error code. Treat it as a file only
        # when it is a declared path; slash/dot paths are independently useful.
        if "/" not in normalized and "." not in normalized and normalized not in allowed:
            continue
        if normalized not in paths:
            paths.append(normalized)
    return paths


def _is_allowed_diagnostic(path: str, allowed: set[str]) -> bool:
    """Allow absolute diagnostic paths that unambiguously end at an allowed file."""
    return any(path == candidate or path.endswith("/" + candidate) for candidate in allowed)


def gate_failure_summary(node: Node, output: str) -> str:
    """Describe whether a failed node gate appears reachable from its allowlist."""
    allowed = {path.removeprefix("./") for path in node.files}
    paths = _diagnostic_paths(output, allowed)
    outside = [path for path in paths if not _is_allowed_diagnostic(path, allowed)]
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "(no output)")
    context = f"$ {node.test_cmd}: {first_line[:240]}"

    if outside:
        return (
            "gate reported diagnostics outside this node's allowlist "
            f"({', '.join(outside[:3])}) — likely a project-wide gate or pre-existing "
            f"baseline error. {context}"
        )
    if paths:
        return f"gate reported diagnostics within this node's allowlist. {context}"
    return (
        "gate failed with no identifiable diagnostic file paths — scope is project-wide "
        f"or unknown-scope. {context}"
    )


# Dogfood / last-resort safety net so director's own Python byproducts stay clean
# even if a target's .gitignore is incomplete. Real cross-stack generality comes
# from .gitignore-derivation + the [repository].ignore config key.
DEFAULT_IGNORE: tuple[str, ...] = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.pyc",
    "*.pyo",
)


def _read_gitignore(worktree: Path) -> list[str]:
    """Read worktree/.gitignore and return cleaned pattern list."""
    try:
        lines = (worktree / ".gitignore").read_text().splitlines()
    except OSError:
        return []

    patterns: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            continue
        # Strip single leading / and single trailing /
        p = stripped
        if p.startswith("/"):
            p = p[1:]
        if p.endswith("/"):
            p = p[:-1]
        if p:
            patterns.append(p)
    return patterns


def build_ignore_matcher(worktree: Path, cfg: Config) -> Callable[[str], bool]:
    """Assemble ONE pattern list and return a closure that matches paths."""
    patterns = list(DEFAULT_IGNORE)

    # Config [repository].ignore patterns
    raw = cfg.repository.get("ignore", [])
    if isinstance(raw, list):
        patterns.extend(p for p in raw if isinstance(p, str))

    # .gitignore-derived patterns
    patterns.extend(_read_gitignore(worktree))

    def match(path: str) -> bool:
        basename = path.rsplit("/", 1)[-1]
        segments = path.split("/")
        for pat in patterns:
            if "/" in pat:
                if fnmatch.fnmatch(path, pat):
                    return True
            else:
                if fnmatch.fnmatch(basename, pat):
                    return True
                if pat in segments:
                    return True
        return False

    return match


def _run(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    o = proc.run_shell(cmd, cwd, timeout)
    return (
        (124, f"(gate command timed out after {timeout}s: {cmd})")
        if o.timed_out
        else (o.returncode, o.output)
    )


def configured_gates(commands: Mapping[str, str]) -> tuple[Gate, ...]:
    """Normalize configured commands into executable gates in declaration order."""
    return tuple(
        Gate(name=name, command=command.strip())
        for name, command in commands.items()
        if command.strip()
    )


def run_gates(gates: Iterable[Gate], cwd: Path, timeout: int) -> GateResult:
    """Run arbitrary gates and report failures by their user-defined names."""
    failures, detail = [], []
    for gate in gates:
        rc, out = _run(gate.command, cwd, timeout)
        if rc != 0:
            failures.append(gate.name)
            detail.append(gate_failure_detail(gate.command, out))
    return GateResult(not failures, failures, "\n".join(detail))


def test_files_intact(node: Node, worktree: Path) -> list[str]:
    """Test files the executor must not have touched. Returns the paths whose
    on-disk hash no longer matches the contract captured at plan time. This makes
    the executor's watch-it-fail mandate enforceable, not advisory: a node that
    edited its own tests can never be marked done."""
    tampered = []
    for path, expected in (node.test_hashes or {}).items():
        fp = worktree / path
        actual = hashlib.sha256(fp.read_bytes()).hexdigest() if fp.exists() else None
        if actual != expected:
            tampered.append(path)
    return tampered


def node_gate(node: Node, worktree: Path, cfg: Config) -> GateResult:
    timeout = cfg.node_timeout
    failures, detail = [], []

    # red-green hardening: the contract (test files) must be byte-for-byte intact.
    tampered = test_files_intact(node, worktree)
    if tampered:
        return GateResult(
            False,
            ["test files modified"],
            "The executor changed the contract (test files): "
            + ", ".join(sorted(tampered))
            + ". Tests are immutable — implement the source instead.",
        )

    rc, out = _run(node.test_cmd, worktree, timeout)
    if rc != 0:
        failures.append("node tests")
        return GateResult(
            False,
            failures,
            gate_failure_detail(node.test_cmd, out),
            gate_failure_summary(node, out),
        )

    # allowlist: only node.files may have changed (tests are committed → any edit
    # to them shows as out-of-scope and is rejected here)
    allowed = set(node.files)
    changed = gitutil.changed_paths(worktree)
    match = build_ignore_matcher(worktree, cfg)
    out_of_scope = [p for p in changed if p not in allowed and not match(p)]
    if out_of_scope:
        failures.append("out-of-scope edits")
        detail.append(
            "Modified files outside the allowlist (revert these): "
            + ", ".join(sorted(out_of_scope))
            + f"\nAllowed: {sorted(allowed)}"
        )
        return GateResult(False, failures, "\n".join(detail))

    # flake control (Phase 3): a node that passed once must pass again. Re-run the
    # tests `flake_runs - 1` more times; any nonzero result means the suite is
    # flaky (order-dependent, time/random-sensitive, or relies on the first run's
    # side effects) and the node is NOT safe to merge.
    for i in range(2, cfg.flake_runs + 1):
        rc2, out2 = _run(node.test_cmd, worktree, timeout)
        if rc2 != 0:
            return GateResult(
                False,
                ["flaky tests"],
                f"Tests passed once but FAILED on re-run {i}/{cfg.flake_runs} — "
                "the suite is flaky and the node cannot merge.\n"
                + gate_failure_detail(node.test_cmd, out2),
                "tests passed once but failed on re-run — the node is flaky. "
                + gate_failure_summary(node, out2),
            )

    return GateResult(True)


def integration_gate(repo: Path, cfg: Config) -> GateResult:
    return run_gates(configured_gates(cfg.gates), repo, cfg.node_timeout)
