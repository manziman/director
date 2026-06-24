"""Core data structures: the task DAG (Plan/Node) and per-node run State."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Node:
    """One atomic unit of work. `spec` must be self-contained — readable by the
    executor with zero other context."""

    id: str
    title: str
    spec: str
    files: list[str]  # allowlist: the ONLY files the executor may modify
    depends_on: list[str] = field(default_factory=list)
    test_cmd: str = ""  # command that gates this node (nonzero = fail)
    tests: list[str] = field(default_factory=list)  # test file paths (test-author writes these)
    estimated_difficulty: str = "medium"  # easy | medium | hard
    # sha256 of each test file, captured by director once tests are authored (NOT
    # emitted by the planner). The node gate refuses to pass if a test file's hash
    # changed — the executor may not edit the contract. See gates.test_files_intact.
    test_hashes: dict = field(default_factory=dict)  # {test_path: sha256}

    @staticmethod
    def from_dict(d: dict) -> Node:
        # Tolerate common field-name drift from different planner models.
        spec = d.get("spec") or d.get("description") or d.get("desc")
        files = d.get("files") or d.get("files_to_modify") or d.get("file_allowlist") or []
        tests = d.get("tests") or d.get("test_files")
        if tests is None:
            tf = d.get("test_file") or d.get("test")
            tests = [tf] if isinstance(tf, str) else (tf or [])
        if spec is None:
            raise KeyError(f"node {d.get('id')!r} has no spec/description")
        return Node(
            id=str(d["id"]),
            title=d.get("title", str(d["id"])),
            spec=spec,
            files=list(files),
            depends_on=[str(x) for x in d.get("depends_on", [])],
            test_cmd=d.get("test_cmd", ""),
            tests=list(tests),
            estimated_difficulty=d.get("estimated_difficulty", "medium"),
            test_hashes=dict(d.get("test_hashes", {})),
        )


@dataclass
class Plan:
    job_id: str
    task: str
    repo: str
    created_at: str
    job_branch: str
    nodes: list[Node] = field(default_factory=list)

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2)

    @staticmethod
    def from_json(text: str) -> Plan:
        d = json.loads(text)
        return Plan(
            job_id=d["job_id"],
            task=d["task"],
            repo=d["repo"],
            created_at=d["created_at"],
            job_branch=d["job_branch"],
            nodes=[Node.from_dict(n) for n in d["nodes"]],
        )


# Node lifecycle statuses persisted in .director/state.json (resumable).
PENDING, RUNNING, DONE, ESCALATED, FAILED = "pending", "running", "done", "escalated", "failed"


@dataclass
class NodeState:
    id: str
    status: str = PENDING
    attempts: int = 0  # executor-tier attempts used
    tier_used: str | None = None  # "executor" | "escalation"
    model_used: str | None = None
    escalated: bool = False
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0})
    cost_usd: float = 0.0
    error: str | None = None
    worktree: str | None = None
    # Phase 2.5 two-stage review
    review_stage_two: bool = False  # did the conditional code-quality review run?
    review_blocks: int = 0  # # of attempts re-opened by a critical finding
    review_summary: str | None = None  # reviewer's last one-line verdict summary
    # Phase 3 measurement
    wall_secs: float = 0.0  # wall time for the node
    watch_it_fail: str | None = None  # "observed" | "not_observed" | "unknown"
    flake_failed: bool = False  # a flake re-run failed this node on some attempt
