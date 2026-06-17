"""Prover native handlers — the deterministic DAG citizens of ``lean-prove``.

This module registers the Lean prover's four native handlers (build plan §1 / Phase 4)
on import, mirroring how ``crews.native`` self-registers its ``echo`` handler:

  - ``retrieve``       — Path A: pull applicable banked lemmas for a sub-goal and write
                         the top candidate (+ ranked next-best list) to the blackboard.
  - ``skeleton_check`` — type-check the decomposer's scaffold in ``skeleton`` mode (the
                         have-chain composes to the target; ``sorry`` elaborates — P1).
  - ``verify``         — the native CONTROLLER (§1a): the kernel is the verdict; the
                         handler owns the deterministic routing (Path-A next-best vs.
                         Path-B repair) and the bounded repair loop, delegating ONLY the
                         repair *proposal* to the ``repair`` agent. Never an LLM verdict.
  - ``bank``           — assemble the sorry-free ``artifacts/result.lean`` from the
                         scaffold + discharged sub-goals, full-verify it, and store each
                         winning lemma (loud writes — surfaces a failed bank into the
                         node result).

Fan-out across multiple ``sorry`` sub-goals is expressed as ordinary DAG nodes sharing
one blackboard, namespaced by sub-goal id (see :func:`_subgoal_id` / :func:`_bb_key`):
``retrieve`` ‖ ``synthesize`` for a sub-goal share an upstream so they run in one wave
(Path A ‖ Path B for free), and ``verify`` lists both as upstream so it waits for both.
This is preferred over the subworkflow hand-off (which is ``result.md``/blackboard-
isolated and awkward for proof-carrying); the subworkflow seam stays available and is
exercised by ``test_subworkflow.py``.

The oracle, imported BY NAME
----------------------------
``verify_lean`` is imported by name into this module so tests can patch it where it is
used: ``mock_lean(targets=("hyperion.crews.lean_handlers.verify_lean",))``. Likewise
:func:`propose_repair` is a module-level seam tests patch with an ``AsyncMock`` to assert
the repair agent is delegated to — without an LLM, and without ever letting a repair
proposal fake a pass (only ``verify_lean`` sets ``ok``).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from hyperion.config import settings
from hyperion.crews.native import NativeNodeCtx, register_native_handler
from hyperion.crews.plan_contract import Subtask, parse_plan
from hyperion.memory import lemma_bank
from hyperion.tools.lean_verify import verify_lean
from hyperion.tools.lemma_retrieval import retrieve_applicable_lemmas

logger = logging.getLogger(__name__)


class ProofFailed(RuntimeError):
    """Raised by ``verify`` when no path closes the sub-goal after Path-A candidates
    and the bounded repair loop are exhausted. Propagates through the runner's normal
    try/except into a ``failed`` run result — a sub-goal that cannot be discharged must
    fail the run cleanly, never silently report ``done`` with an undischarged ``sorry``."""


# ---------------------------------------------------------------------------
# Sub-goal resolution + blackboard key namespacing
# ---------------------------------------------------------------------------


def _subgoal_id(ctx: NativeNodeCtx) -> str:
    """The sub-goal id this node operates on.

    Convention: a prover native node's ``instruction`` (when set) names the sub-goal id
    it handles, so the same handler can be instantiated once per ``sorry`` (each node
    carrying a different id). When unset, fall back to the first active subtask's id
    (the single-sub-goal happy path of the shipped ``lean-prove`` workflow), else ``"0"``.
    """
    if ctx.node.instruction:
        return ctx.node.instruction.strip()
    subs = parse_plan(ctx.task_id).active_subtasks()
    return subs[0].id if subs else "0"


def _subgoal(ctx: NativeNodeCtx, sg_id: str) -> Optional[Subtask]:
    """The :class:`Subtask` for ``sg_id`` from the active plan option, or None."""
    for s in parse_plan(ctx.task_id).active_subtasks():
        if s.id == sg_id:
            return s
    return None


def _goal_type(ctx: NativeNodeCtx, sg_id: str) -> str:
    """The Lean type of sub-goal ``sg_id`` (the retrieval query + synthesis target).

    Prefers the plan contract's ``lean_type``; degrades to the run request (the target
    theorem) when the plan has no typed sub-goal — a degraded query still runs.
    """
    sub = _subgoal(ctx, sg_id)
    if sub and sub.lean_type:
        return sub.lean_type
    return ctx.request


def _bb_key(base: str, sg_id: str) -> str:
    """Blackboard key for ``base`` namespaced to sub-goal ``sg_id`` (e.g. ``candidate_a:0``).

    Namespacing lets multiple sub-goal pipelines share one blackboard without colliding,
    so a workflow can fan out N sub-goals as N node-triples over a single context store.
    """
    return f"{base}:{sg_id}"


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


def _candidate_from_lemma(goal_type: str, lemma: dict[str, Any]) -> dict[str, Any]:
    """Assemble a verifiable Path-A candidate from a retrieved lemma payload.

    Produces a self-contained ``full``-mode source that discharges ``goal_type`` using
    the lemma's proof term. The exact source string is a heuristic refined on the
    live-Lean path (the offline gate mocks ``verify_lean`` and does not depend on it);
    what matters structurally is that ``proof_term`` carries the lemma's verified proof.
    """
    proof_term = lemma.get("proof_term", "") or "by exact?"
    statement = lemma.get("statement", "") or f"example : {goal_type}"
    source = f"example : {goal_type} := {proof_term}"
    return {
        "source": source,
        "statement": statement,
        "proof_term": proof_term,
        "origin": "retrieve",
        "lean_type": lemma.get("lean_type") or goal_type,
    }


# ---------------------------------------------------------------------------
# repair proposal — the one generative sub-step (§1a)
# ---------------------------------------------------------------------------


async def propose_repair(goal: str, candidate_source: str, errors: list[str]) -> str:
    """Ask the ``repair`` agent for ONE revised candidate that closes ``goal``.

    A scoped, structured LLM call (à la ``runner._summarize_context``) that reads its
    model and persona from the ``repair`` agent record — so the model/prompt stay
    operator-configurable in JSON without the weight of a CrewAI crew or a back-edge in
    the DAG. The verify controller owns the loop; this makes exactly one proposal.

    Returns the revised Lean source (falls back to the unchanged candidate on any error,
    so a flaky proxy degrades to "re-check the same source" rather than crashing the
    controller). The kernel still judges the result on the very next line — a proposal
    can never fake a pass.

    The §1a upgrade path: swap this body for a fully autonomous repair agent that owns
    its own inner loop (calling ``lean_verify`` as a tool) — same seam, only the body
    changes.
    """
    from hyperion.agents.registry import load_agent

    try:
        record = load_agent("repair")
    except Exception as exc:  # missing record must not crash the controller
        logger.warning("propose_repair: could not load 'repair' agent (%s)", exc)
        return candidate_source

    system = f"{record.role}\n\n{record.goal}\n\n{record.backstory}"
    user = (
        f"Goal type:\n{goal}\n\n"
        f"Candidate proof that FAILED to type-check:\n{candidate_source}\n\n"
        f"Kernel diagnostics:\n" + "\n".join(errors or ["(no diagnostics)"]) + "\n\n"
        "Return ONLY the full revised Lean 4 source."
    )
    try:
        from openai import OpenAI

        client = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
        resp = client.chat.completions.create(
            model=record.model_alias,
            temperature=record.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or candidate_source
    except Exception as exc:
        logger.warning("propose_repair: LLM call failed (%s) — re-checking unchanged", exc)
        return candidate_source


# ---------------------------------------------------------------------------
# retrieve — Path A
# ---------------------------------------------------------------------------


async def retrieve_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Path A: source applicable banked lemmas for the sub-goal and stage candidates.

    Writes ``candidate_a:<sg>`` (the top applier, or None when the bank yields nothing)
    and ``candidates_a:<sg>`` (the full ranked list, so ``verify`` can advance to a
    next-best match on a both-fail) to the blackboard. Fail-soft throughout: an empty /
    degraded bank simply stages no Path-A candidate (Path B can still close the goal).
    """
    sg_id = _subgoal_id(ctx)
    goal_type = _goal_type(ctx, sg_id)
    lemmas = retrieve_applicable_lemmas(goal_type)
    candidates = [_candidate_from_lemma(goal_type, lem) for lem in lemmas]
    top = candidates[0] if candidates else None
    ctx.put(_bb_key("candidate_a", sg_id), top)
    ctx.put(_bb_key("candidates_a", sg_id), candidates)
    ctx.progress(f"[retrieve] {sg_id}: {len(candidates)} applicable lemma(s)")
    return {
        "handler": "retrieve",
        "subgoal": sg_id,
        "n_candidates": len(candidates),
        "has_candidate": top is not None,
    }


# ---------------------------------------------------------------------------
# skeleton_check — P1 scaffold type-check (skeleton mode)
# ---------------------------------------------------------------------------


async def skeleton_check_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Type-check the decomposer's scaffold in ``skeleton`` mode (``sorry`` permitted).

    Confirms the have-chain composes to the target before any sub-goal sourcing. Routes
    on the load-bearing ``infra_ok`` flag exactly like every verifier caller: a verifier
    outage is inconclusive (``infra_ok=False``) and is NOT treated as a scaffold failure.
    Records the verdict to the blackboard; a real ``ok=False`` is surfaced in the result
    (the decomposer can be revised via the runner's plan-revision flow) but does not
    crash the node.
    """
    scaffold = parse_plan(ctx.task_id).scaffold
    if not scaffold:
        ctx.put("skeleton_ok", None)
        return {"handler": "skeleton_check", "ok": None, "reason": "no scaffold in plan"}
    res = verify_lean(scaffold, mode="skeleton")
    if not res["infra_ok"]:
        # Inconclusive ≠ failure: degrade to "proceed" rather than blocking on a blip.
        ctx.put("skeleton_ok", None)
        return {"handler": "skeleton_check", "ok": None, "infra_ok": False,
                "errors": res["errors"]}
    ctx.put("skeleton_ok", res["ok"])
    return {"handler": "skeleton_check", "ok": res["ok"], "errors": res["errors"]}


# ---------------------------------------------------------------------------
# verify — the native controller (§1a)
# ---------------------------------------------------------------------------


def _full_verdict(source: str) -> tuple[bool, list[str]]:
    """Run the kernel in ``full`` mode; return (closed, errors).

    ``closed`` is True ONLY on a real ``ok=True`` verdict. An infra-down result
    (``infra_ok=False``) is NOT a pass — the verdict is load-bearing ground truth, so we
    never discharge a goal on a verifier we could not reach.
    """
    res = verify_lean(source, mode="full")
    if not res["infra_ok"]:
        return False, res["errors"]
    return bool(res["ok"]), res["errors"]


async def verify_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """The native controller: kernel verdict + deterministic routing + bounded repair.

    Exploit-first routing (build plan §1a):
      1. Try each Path-A candidate (top, then next-best) in ranked order — first that
         closes wins.
      2. Else Path B: check the synthesized candidate; on failure, run the repair loop —
         delegate ONE proposal per iteration to the ``repair`` agent (:func:`propose_repair`)
         and re-check it with the kernel, up to ``settings.cap_repair_iters``.
      3. Exhausted ⇒ raise :class:`ProofFailed` (fail the run cleanly; never report a
         discharge that didn't happen).

    The verdict is ALWAYS the kernel's: an LLM repair can be arbitrarily creative and
    still cannot hallucinate a pass, because every proposal is checked on the next line.
    Writes the winner + the full routing trace to the blackboard for the thesis log.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    decision: dict[str, Any] = {
        "subgoal": sg_id, "winner_path": None, "a_attempts": 0,
        "repair_iters": 0, "verdicts": [],
    }

    def _record(path: str, closed: bool) -> None:
        decision["verdicts"].append({"path": path, "ok": closed})

    winner: Optional[dict[str, Any]] = None

    # ---- Path A: top candidate, then next-best, in ranked order ----
    a_list: list[dict[str, Any]] = []
    top = ctx.get(_bb_key("candidate_a", sg_id))
    if top:
        a_list.append(top)
    for cand in ctx.get(_bb_key("candidates_a", sg_id), []) or []:
        if cand not in a_list:
            a_list.append(cand)
    for cand in a_list:
        decision["a_attempts"] += 1
        closed, _errors = _full_verdict(cand["source"])
        _record("A", closed)
        if closed:
            winner = {**cand, "path": "A"}
            decision["winner_path"] = "A"
            break

    # ---- Path B: synthesized candidate, then the bounded repair loop ----
    if winner is None:
        cb = ctx.get(_bb_key("candidate_b", sg_id))
        if cb:
            closed, errors = _full_verdict(cb["source"])
            _record("B", closed)
            if closed:
                winner = {**cb, "path": "B"}
                decision["winner_path"] = "B"
            else:
                cur_source = cb["source"]
                for _ in range(settings.cap_repair_iters):
                    decision["repair_iters"] += 1
                    cur_source = await propose_repair(goal, cur_source, errors)
                    closed, errors = _full_verdict(cur_source)
                    _record("B-repair", closed)
                    if closed:
                        winner = {
                            "source": cur_source,
                            "statement": cb.get("statement", ""),
                            "proof_term": cur_source,
                            "origin": "repair",
                            "lean_type": cb.get("lean_type") or goal,
                            "path": "B",
                        }
                        decision["winner_path"] = "B"
                        break

    ctx.put(_bb_key("verify_decision", sg_id), decision)
    if winner is None:
        # Clean failure: record the trace, then fail the run (no faked discharge).
        ctx.put(_bb_key("discharged", sg_id), None)
        ctx.progress(f"[verify] {sg_id}: no path closed the goal (gave up)")
        raise ProofFailed(
            f"sub-goal {sg_id!r}: no candidate closed after "
            f"{decision['a_attempts']} Path-A attempt(s) and "
            f"{decision['repair_iters']} repair iteration(s)"
        )

    winner["lean_type"] = winner.get("lean_type") or goal
    ctx.put(_bb_key("discharged", sg_id), winner)
    ctx.progress(f"[verify] {sg_id}: discharged via Path {winner['path']}")
    return {
        "handler": "verify",
        "subgoal": sg_id,
        "ok": True,
        "winner_path": winner["path"],
        "decision": decision,
    }


# ---------------------------------------------------------------------------
# bank — assemble result.lean + store winners (loud)
# ---------------------------------------------------------------------------


def _assemble(scaffold: str, subtasks: list[Subtask], discharged: dict[str, dict]) -> str:
    """Substitute each discharged sub-goal's proof into the scaffold's ``sorry`` holes.

    Replaces the first remaining ``sorry`` token with each sub-goal's proof term, in
    subtask order, so the have-chain that the skeleton check accepted becomes a closed
    proof. Sub-goals with no discharge keep their ``sorry`` (the final full-mode verify
    will then reject the artifact — the loss is not hidden).
    """
    result = scaffold
    for sub in subtasks:
        win = discharged.get(sub.id)
        if not win:
            continue
        proof = win.get("proof_term") or win.get("source") or ""
        result = result.replace("sorry", proof, 1)
    return result


async def bank_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Assemble the sorry-free ``artifacts/result.lean`` and bank each winning lemma.

    Reads the scaffold + active subtasks from the plan and the per-sub-goal ``discharged``
    winners from the blackboard, stitches them into the scaffold, writes
    ``artifacts/result.lean``, full-verifies the assembled proof (the final ground-truth
    gate), then stores each winner in the lemma bank. Bank writes are LOUD (risk #4): a
    failed ``store_lemma`` is surfaced in the node result (``bank_failures``) rather than
    swallowed, because a lost verified lemma stalls the snowball.
    """
    plan = parse_plan(ctx.task_id)
    subtasks = plan.active_subtasks()
    discharged = {s.id: ctx.get(_bb_key("discharged", s.id)) for s in subtasks}
    discharged = {k: v for k, v in discharged.items() if v}

    scaffold = plan.scaffold or ""
    assembled = _assemble(scaffold, subtasks, discharged) if scaffold else ""

    # Final ground-truth gate on the assembled proof.
    final_ok: Optional[bool] = None
    final_errors: list[str] = []
    if assembled:
        res = verify_lean(assembled, mode="full")
        final_ok = res["ok"] if res["infra_ok"] else None
        final_errors = res["errors"]
        result_path = settings.tasks_dir / ctx.task_id / "artifacts" / "result.lean"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(assembled, encoding="utf-8")

    # Store each winning lemma (loud writes — surface failures).
    bank_failures: list[dict[str, str]] = []
    banked = 0
    for sg_id, win in discharged.items():
        sub = next((s for s in subtasks if s.id == sg_id), None)
        store = lemma_bank.store_lemma(
            win.get("statement", "") or sg_id,
            win.get("proof_term", ""),
            source_goal=ctx.request,
            verification_mode="full",
            lean_type=(sub.lean_type if sub else None) or win.get("lean_type"),
        )
        if store["ok"]:
            banked += 1
        else:
            bank_failures.append({"subgoal": sg_id, "error": store["error"] or "unknown"})

    if bank_failures:
        logger.error("bank: %d lemma write(s) failed: %s", len(bank_failures), bank_failures)

    ctx.progress(f"[bank] assembled result.lean; banked {banked}/{len(discharged)} lemma(s)")
    return {
        "handler": "bank",
        "ok": final_ok,
        "errors": final_errors,
        "n_discharged": len(discharged),
        "n_banked": banked,
        "bank_failures": bank_failures,
    }


# ---------------------------------------------------------------------------
# Registration (mirrors crews.native's echo registration)
# ---------------------------------------------------------------------------

register_native_handler("retrieve", retrieve_handler)
register_native_handler("skeleton_check", skeleton_check_handler)
register_native_handler("verify", verify_handler)
register_native_handler("bank", bank_handler)
