"""Thesis-curve read-out — aggregate the Phase-5 triple logs into the experiment's claim.

The Phase-5 ``compare`` step writes one ``(retrieved, synthesized, winner)`` :class:`TripleLog`
per sub-goal to the blackboard (``triple_log:<sg>``). That stream IS the thesis dataset. This
module reads it back (across many runs / task dirs) and computes the read-out:

  - **solved-rate** — fraction of sub-goals that any path closed;
  - **Path-A win-rate** — fraction of closed sub-goals won by *retrieval* (the bank);
  - **retrieval-beats-synthesis-in-contest** — among genuine A-vs-B contests (``compared``),
    how often the banked lemma was preferred;
  - the **running curve** — cumulative Path-A win-rate as sub-goals are processed in order.

The thesis claim (baseline §5 / build-plan Post-work #1): as the bank fills, synthesizer
win-rate falls and retrieval win-rate climbs ⇒ reuse transfers ⇒ the snowball is real. This is
a read-only aggregator over run history; it is never in the hot path. Plotting is left to the
caller (it returns plain numbers / a curve), so the module stays dependency-free.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from hyperion.config import settings


def load_triples(task_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """Load every ``triple_log:<sg>`` record from the given task ids (or all task dirs).

    Fail-soft: a missing / unreadable ``context.json`` is skipped. Records are returned in
    (task, sub-goal-key) order — a stable proxy for "the order sub-goals were proved" that the
    running curve consumes.
    """
    if task_ids is None:
        base = settings.tasks_dir
        task_ids = [p.name for p in base.iterdir() if p.is_dir()] if base.exists() else []

    triples: list[dict[str, Any]] = []
    for tid in sorted(task_ids):
        ctx_path = settings.tasks_dir / tid / "context.json"
        try:
            data = json.loads(ctx_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in sorted(data):
            if key.startswith("triple_log:") and isinstance(data[key], dict):
                triples.append(data[key])
    return triples


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


def aggregate(triples: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate triple logs into the thesis read-out metrics. Pure.

    All rates are guarded against division by zero (empty input ⇒ 0.0).
    """
    n = len(triples)
    solved = sum(1 for t in triples if t.get("winner_path"))
    path_a = sum(1 for t in triples if t.get("winner_path") == "A")
    path_b = sum(1 for t in triples if t.get("winner_path") == "B")
    contests = [t for t in triples if t.get("compared")]
    a_in_contest = sum(1 for t in contests if t.get("winner_path") == "A")

    # Reuse depth — the breadth-vs-depth axis. Measured only over Path-A wins (a synthesis
    # win has no reuse depth). depth==1 everywhere ⇒ breadth (retrieval keeps firing on the
    # same one-lemma move); a rising mean / a populated depth>=2 bucket ⇒ the bank compounds.
    a_depths = [int(t.get("reuse_depth") or 0) for t in triples if t.get("winner_path") == "A"]
    histogram: dict[int, int] = {}
    for d in a_depths:
        histogram[d] = histogram.get(d, 0) + 1

    # Dual value claim (weak-prover gate). Among Path-A wins: how many were *necessary* (no
    # eligible weak Path B existed) vs merely *preferred* (a weak Path B also closed but A won
    # the compare). ``n_b_gated`` counts goals a full-strength prover could solve but the weak
    # one couldn't — the gap the bank fills.
    a_wins = [t for t in triples if t.get("winner_path") == "A"]
    a_necessary = sum(1 for t in a_wins if not t.get("synthesized_verified"))
    n_b_gated = sum(1 for t in triples if t.get("path_b_gated"))
    return {
        "n_subgoals": n,
        "solved": solved,
        "solved_rate": (solved / n) if n else 0.0,
        "path_a_wins": path_a,
        "path_b_wins": path_b,
        "path_a_win_rate": (path_a / solved) if solved else 0.0,
        "n_contests": len(contests),
        "retrieval_beats_synthesis_in_contest": (a_in_contest / len(contests)) if contests else 0.0,
        "mean_reuse_depth": (sum(a_depths) / len(a_depths)) if a_depths else 0.0,
        "max_reuse_depth": max(a_depths) if a_depths else 0,
        "depth_histogram": histogram,
        "path_a_necessary": a_necessary,
        "path_a_necessary_rate": (a_necessary / len(a_wins)) if a_wins else 0.0,
        "n_path_b_gated": n_b_gated,
    }


def aggregate_concepts(concepts: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate synthesized-concept reuse/certification metrics. Pure."""
    n = len(concepts)
    certified = [
        c for c in concepts
        if int(c.get("necessity_hits") or 0) >= settings.concept_promote_k
        or c.get("provisional") is False
    ]
    banked = [c for c in concepts if c.get("bank_id") or c.get("banked")]
    hits = [int(c.get("necessity_hits") or 0) for c in concepts]
    return {
        "n_concepts": n,
        "banked_concepts": len(banked),
        "certified_reusable_concepts": len(certified),
        "certified_reusable_rate": (len(certified) / n) if n else 0.0,
        "total_necessity_hits": sum(hits),
        "max_necessity_hits": max(hits) if hits else 0,
        "provisional_concepts": sum(1 for c in concepts if c.get("provisional", True)),
    }


def running_curve(triples: list[dict[str, Any]]) -> list[float]:
    """Cumulative Path-A (retrieval) win-rate after each *solved* sub-goal, in order.

    The snowball signal: an upward trend means retrieval is winning more as the bank fills.
    Only solved sub-goals advance the curve (an unsolved goal has no winner to attribute).
    """
    curve: list[float] = []
    a = 0
    solved = 0
    for t in triples:
        wp = t.get("winner_path")
        if not wp:
            continue
        solved += 1
        if wp == "A":
            a += 1
        curve.append(a / solved)
    return curve


def depth_curve(triples: list[dict[str, Any]]) -> list[float]:
    """Cumulative mean reuse-depth over *Path-A wins*, in order.

    The depth companion to :func:`running_curve`. Win-rate climbing while this stays flat
    at 1.0 is the breadth illusion (reuse keeps firing on the same one-lemma move); this
    trending up is the snowball compounding (goals composing several banked lemmas).
    """
    curve: list[float] = []
    total = 0
    count = 0
    for t in triples:
        if t.get("winner_path") != "A":
            continue
        total += int(t.get("reuse_depth") or 0)
        count += 1
        curve.append(total / count)
    return curve


def format_summary(triples: list[dict[str, Any]], concepts: Optional[list[dict[str, Any]]] = None) -> str:
    """Render the aggregate + running curve as a short text block."""
    agg = aggregate(triples)
    cagg = aggregate_concepts(concepts or [])
    curve = running_curve(triples)
    dcurve = depth_curve(triples)
    hist = ", ".join(f"d{d}:{agg['depth_histogram'][d]}" for d in sorted(agg["depth_histogram"]))
    lines = [
        "── THESIS READ-OUT (over the triple log) ──",
        f"  sub-goals           : {agg['n_subgoals']}",
        f"  solved              : {agg['solved']}  ({agg['solved_rate']:.0%})",
        f"  Path A (retrieval)  : {agg['path_a_wins']}  win-rate {agg['path_a_win_rate']:.0%}",
        f"  Path B (synthesis)  : {agg['path_b_wins']}",
        f"  A-vs-B contests     : {agg['n_contests']}  "
        f"(retrieval preferred {agg['retrieval_beats_synthesis_in_contest']:.0%})",
        f"  reuse depth         : mean {agg['mean_reuse_depth']:.2f}  max {agg['max_reuse_depth']}"
        f"  [{hist or 'none'}]",
        f"  reuse necessity     : {agg['path_a_necessary']}/{agg['path_a_wins']} A-wins had no "
        f"weak Path B ({agg['path_a_necessary_rate']:.0%})  |  B gated out: {agg['n_path_b_gated']}",
        f"  running A win-rate  : {[round(x, 2) for x in curve]}",
        f"  running mean depth  : {[round(x, 2) for x in dcurve]}",
        f"  concepts            : {cagg['n_concepts']} born, "
        f"{cagg['certified_reusable_concepts']} certified reusable "
        f"({cagg['certified_reusable_rate']:.0%}), "
        f"necessity hits {cagg['total_necessity_hits']}",
        "  (thesis: BOTH curves trend UP — win-rate = reuse fires, depth = bank compounds)",
    ]
    return "\n".join(lines)
