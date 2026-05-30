"""
Routing engine (PLAN_UNIFIED.md Phase 2) — fills the runner's route() hook.

Rule-based, deterministic, no extra LLM cost. Evaluates each active work-stage
agent's trigger against (request, task_type, plan keywords), builds the work DAG
from ``upstream`` edges, topo-sorts (then by ``order``), and rejects cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hyperion.agents.registry import AgentRecord


class RoutingError(ValueError):
    pass


@dataclass
class RoutingResult:
    selected: list[AgentRecord] = field(default_factory=list)  # ordered, ready to run
    skipped: list[dict] = field(default_factory=list)          # [{id, reason}]
    dag: dict[str, list[str]] = field(default_factory=dict)    # id -> upstream ids

    def as_dict(self) -> dict:
        return {
            "selected_agents": [r.id for r in self.selected],
            "skipped": self.skipped,
            "dag": self.dag,
        }


def _trigger_fires(
    record: AgentRecord,
    request: str,
    task_type: str,
    keywords: list[str],
    selected_ids: set[str],
) -> tuple[bool, str]:
    """Return (fires, reason). reason explains a skip (empty when it fires)."""
    trig = record.trigger
    haystack = (request or "").lower()
    kw_set = {k.lower() for k in keywords}

    if trig.type == "always":
        return True, ""
    if trig.type == "keyword":
        for kw in trig.keywords:
            k = kw.lower()
            if k in haystack or k in kw_set:
                return True, ""
        return False, f"no keyword match ({trig.keywords})"
    if trig.type == "task_type":
        if task_type in trig.task_types:
            return True, ""
        return False, f"task_type {task_type!r} not in {trig.task_types}"
    if trig.type == "upstream":
        if trig.upstream and all(u in selected_ids for u in trig.upstream):
            return True, ""
        return False, f"upstream not selected ({trig.upstream})"
    if trig.type == "schedule":
        return False, "schedule-only (fires via scheduler, not per-task routing)"
    return False, f"unknown trigger type {trig.type!r}"


def _topo_sort(records: list[AgentRecord]) -> list[AgentRecord]:
    """Order by upstream edges (deps first), tie-broken by ``order`` then id.
    Only edges to *included* nodes count. Raises RoutingError on a cycle."""
    by_id = {r.id: r for r in records}
    included = set(by_id)
    indeg: dict[str, int] = {r.id: 0 for r in records}
    deps: dict[str, list[str]] = {r.id: [] for r in records}
    for r in records:
        for u in r.trigger.upstream:
            if u in included:
                deps[r.id].append(u)
                indeg[r.id] += 1

    # Kahn's algorithm with deterministic tie-break.
    def sort_key(rid: str) -> tuple[int, str]:
        return (by_id[rid].order, rid)

    ready = sorted([rid for rid in indeg if indeg[rid] == 0], key=sort_key)
    out: list[str] = []
    while ready:
        rid = ready.pop(0)
        out.append(rid)
        for other in records:
            if rid in deps[other.id]:
                indeg[other.id] -= 1
                if indeg[other.id] == 0:
                    ready.append(other.id)
        ready.sort(key=sort_key)
    if len(out) != len(records):
        remaining = included - set(out)
        raise RoutingError(f"Cycle detected in work-stage upstream edges among {sorted(remaining)}")
    return [by_id[rid] for rid in out]


def route_work(
    work_records: list[AgentRecord],
    request: str,
    task_type: str = "mixed",
    keywords: list[str] | None = None,
) -> RoutingResult:
    keywords = keywords or []
    result = RoutingResult()

    active = [r for r in work_records if r.active]
    for r in work_records:
        if not r.active:
            result.skipped.append({"id": r.id, "reason": "inactive"})

    # Trigger evaluation can depend on which agents are already selected (upstream
    # triggers). Iterate to a fixed point so order of declaration doesn't matter.
    selected_ids: set[str] = set()
    skipped_map: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for r in active:
            if r.id in selected_ids:
                continue
            fires, reason = _trigger_fires(r, request, task_type, keywords, selected_ids)
            if fires:
                selected_ids.add(r.id)
                skipped_map.pop(r.id, None)
                changed = True
            else:
                skipped_map[r.id] = reason

    selected_records = [r for r in active if r.id in selected_ids]
    ordered = _topo_sort(selected_records)

    result.selected = ordered
    result.dag = {r.id: [u for u in r.trigger.upstream if u in selected_ids] for r in ordered}
    for r in active:
        if r.id not in selected_ids:
            result.skipped.append({"id": r.id, "reason": skipped_map.get(r.id, "trigger did not fire")})
    return result
