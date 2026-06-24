"""Cost accounting — every model call is tagged with its role and resolved model.

Cost is computed from per-model pricing in config (local endpoints priced at $0
but still counted). Entries are appended to `.director/costs.jsonl` so accounting
survives across `plan` and `run` and is resumable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from director.config import Config


def cost_of(model: str, tokens: dict, cfg: Config) -> float:
    p = cfg.price(model)
    return (tokens.get("input", 0) / 1_000_000) * float(p.get("input", 0.0)) + (
        tokens.get("output", 0) / 1_000_000
    ) * float(p.get("output", 0.0))


@dataclass
class CostEntry:
    role: str
    model: str
    input: int
    output: int
    cost: float
    node: str | None = None
    ts: float = 0.0


class CostLedger:
    """Append-only ledger backed by .director/costs.jsonl."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: list[CostEntry] = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    d = json.loads(line)
                    self.entries.append(CostEntry(**d))

    def record(
        self, *, role: str, model: str, tokens: dict, cfg: Config, node: str | None = None
    ) -> float:
        c = cost_of(model, tokens, cfg)
        e = CostEntry(
            role=role,
            model=model,
            input=int(tokens.get("input", 0)),
            output=int(tokens.get("output", 0)),
            cost=c,
            node=node,
            ts=time.time(),
        )
        self.entries.append(e)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(e.__dict__) + "\n")
        return c

    def total(self) -> float:
        return sum(e.cost for e in self.entries)

    def by_role(self) -> dict[str, dict]:
        return self._group(lambda e: e.role)

    def by_model(self) -> dict[str, dict]:
        return self._group(lambda e: e.model)

    def _group(self, key) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in self.entries:
            g = out.setdefault(key(e), {"input": 0, "output": 0, "cost": 0.0, "calls": 0})
            g["input"] += e.input
            g["output"] += e.output
            g["cost"] += e.cost
            g["calls"] += 1
        return out
