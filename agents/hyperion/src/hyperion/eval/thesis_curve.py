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
    return {
        "n_subgoals": n,
        "solved": solved,
        "solved_rate": (solved / n) if n else 0.0,
        "path_a_wins": path_a,
        "path_b_wins": path_b,
        "path_a_win_rate": (path_a / solved) if solved else 0.0,
        "n_contests": len(contests),
        "retrieval_beats_synthesis_in_contest": (a_in_contest / len(contests)) if contests else 0.0,
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


def format_summary(triples: list[dict[str, Any]]) -> str:
    """Render the aggregate + running curve as a short text block."""
    agg = aggregate(triples)
    curve = running_curve(triples)
    lines = [
        "── THESIS READ-OUT (over the triple log) ──",
        f"  sub-goals           : {agg['n_subgoals']}",
        f"  solved              : {agg['solved']}  ({agg['solved_rate']:.0%})",
        f"  Path A (retrieval)  : {agg['path_a_wins']}  win-rate {agg['path_a_win_rate']:.0%}",
        f"  Path B (synthesis)  : {agg['path_b_wins']}",
        f"  A-vs-B contests     : {agg['n_contests']}  "
        f"(retrieval preferred {agg['retrieval_beats_synthesis_in_contest']:.0%})",
        f"  running A win-rate  : {[round(x, 2) for x in curve]}",
        "  (thesis: this curve trends UP as the bank fills ⇒ the snowball is real)",
    ]
    return "\n".join(lines)
