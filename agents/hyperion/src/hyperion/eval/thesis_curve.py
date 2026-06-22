"""Read-only aggregate over trimmed prover outcomes."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from hyperion.config import settings


def load_outcomes(task_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """Load every ``discharged:<sg>`` record from the given task ids."""
    if task_ids is None:
        base = settings.tasks_dir
        task_ids = [p.name for p in base.iterdir() if p.is_dir()] if base.exists() else []

    outcomes: list[dict[str, Any]] = []
    for tid in sorted(task_ids):
        ctx_path = settings.tasks_dir / tid / "context.json"
        try:
            data = json.loads(ctx_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in sorted(data):
            if key.startswith("discharged:") and isinstance(data[key], dict):
                outcome = {**data[key], "task_id": tid, "subgoal": key.split(":", 1)[1]}
                outcomes.append(outcome)
    return outcomes


def load_concepts(task_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """Load every ``accepted_concept:<sg>`` record from run blackboards."""
    if task_ids is None:
        base = settings.tasks_dir
        task_ids = [p.name for p in base.iterdir() if p.is_dir()] if base.exists() else []

    concepts: list[dict[str, Any]] = []
    for tid in sorted(task_ids):
        ctx_path = settings.tasks_dir / tid / "context.json"
        try:
            data = json.loads(ctx_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in sorted(data):
            if key.startswith("accepted_concept:") and isinstance(data[key], dict):
                concepts.append({**data[key], "task_id": tid, "subgoal": key.split(":", 1)[1]})
    return concepts


def _winner_path(record: dict[str, Any]) -> str | None:
    return record.get("winner_path") or record.get("path")


def aggregate(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate discharged outcome records. Pure."""
    n = len(outcomes)
    solved = sum(1 for o in outcomes if _winner_path(o))
    path_a = sum(1 for o in outcomes if _winner_path(o) == "A")
    path_b = sum(1 for o in outcomes if _winner_path(o) == "B")
    path_c = sum(1 for o in outcomes if _winner_path(o) == "C")

    a_depths = [int(o.get("reuse_depth") or 0) for o in outcomes if _winner_path(o) == "A"]
    histogram: dict[int, int] = {}
    for d in a_depths:
        histogram[d] = histogram.get(d, 0) + 1

    return {
        "n_subgoals": n,
        "solved": solved,
        "solved_rate": (solved / n) if n else 0.0,
        "path_a_wins": path_a,
        "path_b_wins": path_b,
        "path_c_wins": path_c,
        "path_a_win_rate": (path_a / solved) if solved else 0.0,
        "mean_reuse_depth": (sum(a_depths) / len(a_depths)) if a_depths else 0.0,
        "max_reuse_depth": max(a_depths) if a_depths else 0,
        "depth_histogram": histogram,
    }


def aggregate_concepts(concepts: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate synthesized-concept cache metrics. Pure."""
    n = len(concepts)
    banked = [c for c in concepts if c.get("bank_id") or c.get("banked")]
    return {
        "n_concepts": n,
        "banked_concepts": len(banked),
        "banked_concept_rate": (len(banked) / n) if n else 0.0,
    }


def running_curve(outcomes: list[dict[str, Any]]) -> list[float]:
    """Cumulative Path-A win-rate after each solved sub-goal, in order."""
    curve: list[float] = []
    a = 0
    solved = 0
    for o in outcomes:
        wp = _winner_path(o)
        if not wp:
            continue
        solved += 1
        if wp == "A":
            a += 1
        curve.append(a / solved)
    return curve


def depth_curve(outcomes: list[dict[str, Any]]) -> list[float]:
    """Cumulative mean reuse-depth over Path-A wins."""
    curve: list[float] = []
    total = 0
    count = 0
    for o in outcomes:
        if _winner_path(o) != "A":
            continue
        total += int(o.get("reuse_depth") or 0)
        count += 1
        curve.append(total / count)
    return curve


def format_summary(outcomes: list[dict[str, Any]], concepts: Optional[list[dict[str, Any]]] = None) -> str:
    """Render the aggregate + running curve as a short text block."""
    agg = aggregate(outcomes)
    cagg = aggregate_concepts(concepts or [])
    curve = running_curve(outcomes)
    dcurve = depth_curve(outcomes)
    hist = ", ".join(f"d{d}:{agg['depth_histogram'][d]}" for d in sorted(agg["depth_histogram"]))
    lines = [
        "-- PROVER READ-OUT (over discharged outcomes) --",
        f"  sub-goals           : {agg['n_subgoals']}",
        f"  solved              : {agg['solved']}  ({agg['solved_rate']:.0%})",
        f"  Path A (retrieval)  : {agg['path_a_wins']}  win-rate {agg['path_a_win_rate']:.0%}",
        f"  Path B (synthesis)  : {agg['path_b_wins']}",
        f"  Path C (concept)    : {agg['path_c_wins']}",
        f"  reuse depth         : mean {agg['mean_reuse_depth']:.2f}  max {agg['max_reuse_depth']}"
        f"  [{hist or 'none'}]",
        f"  running A win-rate  : {[round(x, 2) for x in curve]}",
        f"  running mean depth  : {[round(x, 2) for x in dcurve]}",
        f"  concepts            : {cagg['n_concepts']} accepted, "
        f"{cagg['banked_concepts']} banked ({cagg['banked_concept_rate']:.0%})",
    ]
    return "\n".join(lines)


# Backward-compatible alias for callers that have not renamed yet.
load_triples = load_outcomes
