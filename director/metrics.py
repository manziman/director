"""Metrics stream (Phase 3) — `.director/metrics.jsonl`.

The hypothesis is falsifiable, so every run must be measurable. This is an
append-only NDJSON stream: one `kind:"node"` record per finished node and one
`kind:"run"` summary record at the end. It is written alongside the cost ledger
(`costs.jsonl`) and run state (`state.json`), and is what `director bench` and any
external analysis read to compare profiles.

Keeping metrics in their own stream (rather than overloading the cost ledger)
means the cost story stays a pure per-call ledger while metrics carry the derived
rates (escalation, stage-two trigger, watch-it-fail) and wall time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class MetricsWriter:
    """Append-only metrics stream backed by .director/metrics.jsonl."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, record: dict) -> None:
        rec = {"ts": time.time(), **record}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def read_records(path: Path) -> list[dict]:
    """Load all metrics records (both kinds) from a metrics.jsonl, oldest first."""
    path = Path(path)
    out: list[dict] = []
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def latest_run(path: Path) -> dict | None:
    """The most recent run-level summary record, if any."""
    runs = [r for r in read_records(path) if r.get("kind") == "run"]
    return runs[-1] if runs else None
