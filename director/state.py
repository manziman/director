"""Resumable run state persisted to .director/state.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from director.models import DONE, PENDING, NodeState


class RunState:
    def __init__(self, path: Path, job_id: str):
        self.path = Path(path)
        self.job_id = job_id
        self.nodes: dict[str, NodeState] = {}

    @classmethod
    def load_or_init(cls, repo: Path, plan) -> RunState:
        path = Path(repo) / ".director" / "state.json"
        rs = cls(path, plan.job_id)
        if path.exists():
            d = json.loads(path.read_text())
            if d.get("job_id") == plan.job_id:
                rs.nodes = {k: NodeState(**v) for k, v in d.get("nodes", {}).items()}
        for n in plan.nodes:
            rs.nodes.setdefault(n.id, NodeState(id=n.id, status=PENDING))
        return rs

    def save(self) -> None:
        # Atomic write-temp-then-replace: concurrent readers (`director ui` polls
        # this file from another process) must never observe a truncated file.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"job_id": self.job_id, "nodes": {k: asdict(v) for k, v in self.nodes.items()}},
            indent=2,
        )
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, self.path)

    def done_ids(self) -> set[str]:
        return {k for k, v in self.nodes.items() if v.status == DONE}

    def __getitem__(self, node_id: str) -> NodeState:
        return self.nodes[node_id]
