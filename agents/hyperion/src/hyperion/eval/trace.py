"""Per-run stage tracer — reconstruct what each prover stage produced.

A prover run leaves a complete record of itself on the durable blackboard
(``context.json``): every stage writes its output under a sub-goal-namespaced key
(``candidate_a:<sg>``, ``candidate_b:<sg>``, ``verified_a/b:<sg>``, ``verify_decision:<sg>``,
``triple_log:<sg>``, ``discharged:<sg>``, ``abstracted:<sg>``), the decomposer writes the
plan/scaffold, and ``bank`` writes ``artifacts/result.lean``. This module reads that record
back and organizes it by sub-goal in pipeline order, so a human (or the thesis harness) can
see exactly what decompose → skeleton_check → retrieve ‖ synthesize → verify → compare →
abstract → bank each produced — without instrumenting the hot path.

:func:`collect_trace` is pure (it takes the blackboard/plan/result as data); :func:`trace_task`
is the thin disk-reading wrapper. :func:`format_trace` renders a readable report.
"""

from __future__ import annotations

from typing import Any, Optional

from hyperion.config import settings
from hyperion.crews.plan_contract import PlanFrontmatter, parse_plan
from hyperion.memory.context_store import context_get

# Stage outputs in pipeline order, keyed by the blackboard base names each stage writes.
_PER_SG_KEYS = (
    "candidate_a", "candidates_a", "candidate_b",
    "verified_a", "verified_b", "verify_decision",
    "concept_context", "stall_errors",
    "escalated", "synthesize_definition", "concept_candidates",
    "verify_concept", "verified_concept", "birth_ablation",
    "accepted_concept", "bank_concept",
    "triple_log", "discharged", "abstracted",
)


def _subgoal_ids(blackboard: dict[str, Any], subtasks: list) -> list[str]:
    """Sub-goal ids to trace: the plan's active subtasks, else inferred from key suffixes."""
    ids = [s.id for s in subtasks]
    if ids:
        return ids
    found: set[str] = set()
    for k in blackboard:
        if ":" in k:
            found.add(k.split(":", 1)[1])
    return sorted(found)


def collect_trace(
    *,
    request: str,
    blackboard: dict[str, Any],
    plan: Optional[PlanFrontmatter],
    result_lean: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble a structured per-stage trace from already-loaded run data. Pure.

    Returns a dict with the run-level fields (``request``/``status``/``scaffold``/
    ``skeleton_ok``/``result_lean``) and ``subgoals``: ``{sg_id: {<stage outputs>}}`` with
    each per-sub-goal blackboard key resolved (missing keys default to None / []).
    """
    subs = plan.active_subtasks() if plan else []
    sub_by_id = {s.id: s for s in subs}
    per_sg: dict[str, dict[str, Any]] = {}
    for sg in _subgoal_ids(blackboard, subs):
        entry: dict[str, Any] = {
            "lean_type": (sub_by_id[sg].lean_type if sg in sub_by_id else None),
        }
        for base in _PER_SG_KEYS:
            entry[base] = blackboard.get(f"{base}:{sg}")
        entry["candidates_a"] = entry.get("candidates_a") or []
        per_sg[sg] = entry
    return {
        "request": request,
        "status": status,
        "scaffold": (plan.scaffold if plan else None),
        "skeleton_ok": blackboard.get("skeleton_ok"),
        "skeleton_errors": blackboard.get("skeleton_errors") or [],
        "final_verify": blackboard.get("final_verify"),
        "subgoals": per_sg,
        "result_lean": result_lean,
    }


def trace_task(task_id: str, *, request: str = "", status: Optional[str] = None) -> dict[str, Any]:
    """Disk-reading wrapper around :func:`collect_trace` for a completed/partial run.

    Reads the whole blackboard, the plan, and ``artifacts/result.lean`` from
    ``settings.tasks_dir`` (patch it in tests / point it at a run dir). Safe to call mid-run.
    """
    blackboard = context_get(task_id) or {}
    plan = parse_plan(task_id)
    result_path = settings.tasks_dir / task_id / "artifacts" / "result.lean"
    result_lean = result_path.read_text(encoding="utf-8") if result_path.exists() else None
    return collect_trace(
        request=request, blackboard=blackboard, plan=plan,
        result_lean=result_lean, status=status,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cand(c: Optional[dict[str, Any]], *, width: int = 48) -> str:
    """One-line summary of a candidate dict."""
    if not c:
        return "∅"
    origin = c.get("origin", "?")
    path = c.get("path", "?")
    lean_type = (c.get("lean_type") or c.get("statement") or "").strip()
    proof = (c.get("proof_term") or c.get("source") or "").strip().replace("\n", " ")
    if len(proof) > width:
        proof = proof[: width - 1] + "…"
    return f"{origin}/{path}  type={lean_type!r}  proof={proof!r}"


def _flag(b: Optional[bool]) -> str:
    return "✓" if b is True else ("∅" if b is None else "✗")


def format_trace(trace: dict[str, Any]) -> str:
    """Render a trace as a readable per-stage, per-sub-goal report."""
    out: list[str] = []
    out.append(f"══ {trace['request']!r}   status={trace.get('status')} ══")
    if trace.get("scaffold"):
        out.append("scaffold:")
        for line in trace["scaffold"].splitlines():
            out.append(f"    {line}")
    out.append(f"skeleton_check: ok={trace.get('skeleton_ok')}")

    for sg, e in trace["subgoals"].items():
        out.append("")
        out.append(f"─ sub-goal {sg}   (type: {e.get('lean_type')!r}) ─")
        n_a = (1 if e.get("candidate_a") else 0) + max(0, len(e.get("candidates_a", [])) - 1)
        out.append(f"  retrieve  (Path A): {n_a} candidate(s); top = {_cand(e.get('candidate_a'))}")
        out.append(f"  synthesize(Path B): {_cand(e.get('candidate_b'))}")
        vd = e.get("verify_decision") or {}
        out.append(
            f"  verify            : mode={vd.get('mode')} a_attempts={vd.get('a_attempts')} "
            f"repair_iters={vd.get('repair_iters')}  "
            f"verified_a={_flag(e.get('verified_a') is not None)} "
            f"verified_b={_flag(e.get('verified_b') is not None)}"
        )
        tl = e.get("triple_log") or {}
        out.append(
            f"  compare           : winner=Path {tl.get('winner_path')}  "
            f"compared={tl.get('compared')}  scores={tl.get('scores')}"
        )
        out.append(
            f"  definition synth  : escalated={bool(e.get('escalated'))} "
            f"candidates={len(e.get('concept_candidates') or [])} "
            f"verified={_flag(e.get('verified_concept') is not None)}"
        )
        ba = e.get("birth_ablation") or {}
        out.append(
            f"  birth ablation    : pass={_flag(ba.get('accept'))} "
            f"concept_id={ba.get('concept_id')}"
        )
        bc = e.get("bank_concept") or {}
        out.append(
            f"  concept bank      : banked={_flag(bc.get('banked'))} "
            f"necessity_hits={(e.get('accepted_concept') or {}).get('necessity_hits')}"
        )
        ab = e.get("abstracted")
        if ab is None:
            out.append("  abstract          : did not fire (no fresh Path-B lemma)")
        else:
            out.append(f"  abstract          : {_cand(ab)}")
        out.append(f"  → discharged      : {_cand(e.get('discharged'))}")

    out.append("")
    rl = trace.get("result_lean")
    if rl is not None:
        out.append(f"result.lean (sorry-free={'sorry' not in rl}):")
        for line in rl.splitlines():
            out.append(f"    {line}")
    else:
        out.append("result.lean: (none written)")
    return "\n".join(out)
