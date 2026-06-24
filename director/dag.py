"""Task-DAG validation and scheduling.

Guarantees that make cheap parallel execution safe:
  - acyclic, with all depends_on referencing real nodes,
  - any two *concurrent* nodes (neither depends on the other, directly or
    transitively) have disjoint file allowlists — so parallel worktrees can never
    produce conflicting edits to the same file,
  - every node has a test_cmd (its gate).
"""

from __future__ import annotations

from director.models import Plan


class DagError(ValueError):
    pass


def _descendants(nodes) -> dict[str, set[str]]:
    """node id -> set of all transitive descendants (nodes that depend on it)."""
    children: dict[str, set[str]] = {n.id: set() for n in nodes}
    for n in nodes:
        for dep in n.depends_on:
            children[dep].add(n.id)
    out: dict[str, set[str]] = {}
    for n in nodes:
        seen: set[str] = set()
        stack = list(children[n.id])
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            stack.extend(children[c])
        out[n.id] = seen
    return out


def topo_order(plan: Plan) -> list[str]:
    """Kahn's algorithm. Raises DagError on a cycle."""
    ids = [n.id for n in plan.nodes]
    indeg = dict.fromkeys(ids, 0)
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for n in plan.nodes:
        for dep in n.depends_on:
            adj[dep].append(n.id)
            indeg[n.id] += 1
    # deterministic order: process ready nodes in their plan order
    ready = [i for i in ids if indeg[i] == 0]
    order: list[str] = []
    while ready:
        ready.sort(key=ids.index)
        cur = ready.pop(0)
        order.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(ids):
        stuck = [i for i in ids if i not in order]
        raise DagError(f"cycle detected among nodes: {', '.join(stuck)}")
    return order


def validate(plan: Plan) -> None:
    """Raise DagError on the first structural problem. Returns None if valid."""
    nodes = plan.nodes
    if not nodes:
        raise DagError("plan has no nodes")

    ids = [n.id for n in nodes]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise DagError(f"duplicate node ids: {', '.join(sorted(dupes))}")
    idset = set(ids)

    for n in nodes:
        bad = [d for d in n.depends_on if d not in idset]
        if bad:
            raise DagError(f"node '{n.id}' depends_on unknown node(s): {', '.join(bad)}")
        if not n.test_cmd.strip():
            raise DagError(f"node '{n.id}' has no test_cmd (every node needs a gate)")
        if not n.files:
            raise DagError(f"node '{n.id}' has an empty file allowlist")

    topo_order(plan)  # raises on cycle

    # concurrent nodes must have disjoint allowlists
    desc = _descendants(nodes)
    by_id = {n.id: n for n in nodes}
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            related = (b in desc[a]) or (a in desc[b])
            if related:
                continue
            overlap = set(by_id[a].files) & set(by_id[b].files)
            if overlap:
                raise DagError(
                    f"concurrent nodes '{a}' and '{b}' share files {sorted(overlap)} "
                    f"— they could conflict in parallel. Add a depends_on or split files."
                )


def ready_nodes(plan: Plan, done: set[str], active: set[str]) -> list[str]:
    """Nodes whose deps are all done and that are neither done nor running."""
    out = []
    for n in plan.nodes:
        if n.id in done or n.id in active:
            continue
        if all(d in done for d in n.depends_on):
            out.append(n.id)
    return out
