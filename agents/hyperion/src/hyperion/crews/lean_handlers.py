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
from hyperion.crews.lemma_compare import build_triple, choose_winner, generality_score
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
# abstraction proposal — the one generative sub-step of `abstract` (§Phase 5 (a))
# ---------------------------------------------------------------------------


async def propose_abstraction(
    statement: str, proof_term: str, lean_type: str
) -> list[dict[str, Any]]:
    """Ask the ``abstractor`` agent for lifted lemmas, ORDERED most-general first.

    The :func:`propose_repair` twin (build plan §Phase 5 decision a): a scoped, structured
    LLM call that reads its model/persona from the ``abstractor`` agent record, so the
    generalization model/prompt stay operator-configurable in JSON without a CrewAI crew or
    a DAG back-edge. The :func:`abstract_handler` controller owns the re-verify + fallback
    loop; this only proposes.

    Returns a list of candidate dicts (``{source, statement, proof_term, lean_type}``)
    ordered from boldest generalization to most conservative — the controller keeps the
    first that still type-checks (the most-general form that re-verifies). Returns ``[]``
    on any error so the controller falls back to the concrete verified lemma; the kernel
    re-verifies every proposal, so an over-abstraction can never sneak into the bank.
    """
    from hyperion.agents.registry import load_agent

    try:
        record = load_agent("abstractor")
    except Exception as exc:  # missing record must not crash the controller
        logger.warning("propose_abstraction: could not load 'abstractor' agent (%s)", exc)
        return []

    system = f"{record.role}\n\n{record.goal}\n\n{record.backstory}"
    user = (
        f"Goal type the lemma was derived for:\n{lean_type}\n\n"
        f"Verified lemma statement:\n{statement}\n\n"
        f"Proof term that closed it:\n{proof_term}\n\n"
        "Return ONLY a JSON array of candidate lemmas, MOST GENERAL FIRST, each "
        '{"source", "statement", "proof_term", "lean_type"}.'
    )
    try:
        import json

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
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("source")]
        return []
    except Exception as exc:
        logger.warning("propose_abstraction: LLM call failed (%s) — no proposals", exc)
        return []


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

    Routing (build plan §1a; RESEARCH/DEPLOY per §Phase 5 decision b):
      1. Try each Path-A candidate (top, then next-best) in ranked order — the first that
         closes is the verified Path-A lemma (``verified_a``).
      2. Path B (always in RESEARCH mode; in DEPLOY only when Path A did not verify):
         check the synthesized candidate; on failure run the repair loop — delegate ONE
         proposal per iteration to the ``repair`` agent (:func:`propose_repair`) and re-check
         it, up to ``settings.cap_repair_iters`` — yielding the verified Path-B lemma
         (``verified_b``).
      3. Nothing verifies ⇒ raise :class:`ProofFailed` (fail the run cleanly; never report
         a discharge that didn't happen).

    DEPLOY (``prover_research_mode`` False, default) is exploit-first: it short-circuits
    once Path A closes and never pays to verify Path B — the historical behavior. RESEARCH
    verifies BOTH so ``compare`` has a genuine A-vs-B contest and ``abstract`` can fire on a
    fresh Path-B lemma even when Path A also closed (anti-starvation, decision e).

    The verdict is ALWAYS the kernel's: an LLM repair can be arbitrarily creative and
    still cannot hallucinate a pass, because every proposal is checked on the next line.
    Writes ``verified_a``/``verified_b`` (the compare inputs), a provisional ``discharged``
    (exploit-first pick — its single-winner contract, finalized later by ``compare``), and
    the full routing trace to the blackboard for the thesis log.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    research = settings.prover_research_mode
    decision: dict[str, Any] = {
        "subgoal": sg_id, "winner_path": None, "a_attempts": 0,
        "repair_iters": 0, "verdicts": [], "mode": "research" if research else "deploy",
    }

    def _record(path: str, closed: bool) -> None:
        decision["verdicts"].append({"path": path, "ok": closed})

    verified_a: Optional[dict[str, Any]] = None
    verified_b: Optional[dict[str, Any]] = None

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
            verified_a = {**cand, "path": "A"}
            break

    # ---- Path B: synthesized candidate, then the bounded repair loop ----
    # RESEARCH: always verify B (the comparison is the experiment). DEPLOY: only when
    # Path A failed to close (exploit-first — don't pay for B once A has won).
    if research or verified_a is None:
        cb = ctx.get(_bb_key("candidate_b", sg_id))
        if cb:
            closed, errors = _full_verdict(cb["source"])
            _record("B", closed)
            if closed:
                verified_b = {**cb, "path": "B"}
            else:
                cur_source = cb["source"]
                for _ in range(settings.cap_repair_iters):
                    decision["repair_iters"] += 1
                    cur_source = await propose_repair(goal, cur_source, errors)
                    closed, errors = _full_verdict(cur_source)
                    _record("B-repair", closed)
                    if closed:
                        verified_b = {
                            "source": cur_source,
                            "statement": cb.get("statement", ""),
                            "proof_term": cur_source,
                            "origin": "repair",
                            "lean_type": cb.get("lean_type") or goal,
                            "path": "B",
                        }
                        break

    # verified_a/verified_b are the compare inputs (anti-starvation reads verified_b).
    ctx.put(_bb_key("verified_a", sg_id), verified_a)
    ctx.put(_bb_key("verified_b", sg_id), verified_b)

    # Provisional winner — exploit-first prefers A; compare finalizes when both verified.
    winner: Optional[dict[str, Any]] = verified_a or verified_b
    if winner is not None:
        decision["winner_path"] = winner["path"]

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
# compare — finalize the winner + write the thesis triple log (§Phase 5 (c)/(d))
# ---------------------------------------------------------------------------


async def compare_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Pick the preferred verified candidate and log the ``(retrieved, synthesized,
    winner)`` triple — the experiment's core measurement (build plan §Phase 5 (c)/(d)).

    Reads the considered candidates (``candidate_a``/``candidate_b``) and what actually
    verified (``verified_a``/``verified_b``, written by :func:`verify_handler`). Delegates
    the choice to the pure, deterministic :func:`lemma_compare.choose_winner` (more general
    / shorter / reuse-first), finalizes ``discharged:<sg>`` to that winner (carrying its
    ``generality_score`` for the bank), and writes the fixed :class:`lemma_compare.TripleLog`
    to ``triple_log:<sg>`` for Post-work's thesis-curve harness. Pure logic lives in
    ``lemma_compare``; this handler is just the blackboard plumbing around it.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    retrieved = ctx.get(_bb_key("candidate_a", sg_id))
    synthesized = ctx.get(_bb_key("candidate_b", sg_id))
    verified_a = ctx.get(_bb_key("verified_a", sg_id))
    verified_b = ctx.get(_bb_key("verified_b", sg_id))

    winner = choose_winner(verified_a, verified_b)
    if winner is not None:
        winner["lean_type"] = winner.get("lean_type") or goal
        winner["generality_score"] = generality_score(winner)
        ctx.put(_bb_key("discharged", sg_id), winner)  # finalize the verify provisional

    mode = "research" if settings.prover_research_mode else "deploy"
    triple = build_triple(
        subgoal=sg_id, goal_type=goal,
        retrieved=retrieved, synthesized=synthesized,
        verified_a=verified_a, verified_b=verified_b,
        winner=winner, mode=mode,
    )
    ctx.put(_bb_key("triple_log", sg_id), triple)
    ctx.progress(
        f"[compare] {sg_id}: winner Path {triple['winner_path']} "
        f"(compared={triple['compared']})"
    )
    return {
        "handler": "compare",
        "subgoal": sg_id,
        "winner_path": triple["winner_path"],
        "compared": triple["compared"],
        "scores": triple["scores"],
    }


# ---------------------------------------------------------------------------
# abstract — the anti-unification abstractor (§Phase 5 (a)/(e); baseline §5/§6.6)
# ---------------------------------------------------------------------------


async def abstract_handler(ctx: NativeNodeCtx) -> dict[str, Any]:
    """Generalize a fresh verified Path-B lemma, re-verify, keep the most-general form.

    The novel module (build plan §Phase 5), built as a native CONTROLLER mirroring
    verify/repair: the deterministic re-verify + most-general-that-type-checks selection +
    over-abstraction rejection/fallback live here; only the generative lift is delegated to
    the ``abstractor`` agent (:func:`propose_abstraction`).

    Anti-starvation trigger (decision e): fires iff ``verified_b:<sg>`` is set — i.e. Path B
    produced a kernel-verified lemma — read INDEPENDENTLY of who won the compare. So when
    RESEARCH mode verified both and compare picked Path A, the bespoke Path-B lemma is still
    generalized into the bank rather than starved. In DEPLOY mode Path B isn't verified once
    A wins ⇒ ``verified_b`` is None ⇒ this cleanly no-ops (nothing fresh to generalize).

    Selection: :func:`propose_abstraction` returns proposals most-general-first; the kernel
    (``full`` mode) re-verifies each in order and the FIRST that type-checks is kept. An
    over-abstraction that no longer type-checks is rejected; if none type-check, fall back
    to the concrete verified lemma. The chosen form is written to ``abstracted:<sg>`` so the
    bank stores the generalized (or fallback) lemma.
    """
    sg_id = _subgoal_id(ctx)
    goal = _goal_type(ctx, sg_id)
    fresh_b = ctx.get(_bb_key("verified_b", sg_id))
    if not fresh_b:
        ctx.put(_bb_key("abstracted", sg_id), None)
        ctx.progress(f"[abstract] {sg_id}: no fresh Path-B lemma — skipped")
        return {"handler": "abstract", "subgoal": sg_id, "fired": False,
                "reason": "no fresh Path-B lemma (verified_b unset)"}

    statement = fresh_b.get("statement", "")
    proof_term = fresh_b.get("proof_term", "")
    lean_type = fresh_b.get("lean_type") or goal
    proposals = await propose_abstraction(statement, proof_term, lean_type)

    chosen: Optional[dict[str, Any]] = None
    n_rejected = 0
    for p in proposals:  # most-general first
        src = p.get("source", "")
        if not src:
            continue
        closed, _errors = _full_verdict(src)
        if closed:
            chosen = {
                "source": src,
                "statement": p.get("statement", statement),
                "proof_term": p.get("proof_term", src),
                "origin": "abstract",
                "lean_type": p.get("lean_type") or lean_type,
                "path": fresh_b.get("path", "B"),
            }
            break
        n_rejected += 1  # over-abstraction rejected — try the next, more conservative one

    if chosen is None:
        # No proposal type-checked (or none offered) → fall back to the concrete lemma.
        chosen = {**fresh_b, "origin": "abstract-fallback"}
        abstracted = False
    else:
        abstracted = True
    chosen["generality_score"] = generality_score(chosen)
    ctx.put(_bb_key("abstracted", sg_id), chosen)
    ctx.progress(
        f"[abstract] {sg_id}: {'abstracted' if abstracted else 'fell back'} "
        f"({n_rejected} over-abstraction(s) rejected)"
    )
    return {
        "handler": "abstract",
        "subgoal": sg_id,
        "fired": True,
        "abstracted": abstracted,
        "n_rejected": n_rejected,
        "origin": chosen["origin"],
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

    # Store each winning lemma (loud writes — surface failures). Prefer the ABSTRACTED
    # form (§Phase 5): the abstractor's most-general type-checking generalization is what
    # grows the bank's reuse, so store it instead of the concrete winner when present.
    # (result.lean above is still assembled from the concrete ``discharged`` proof that
    # fits the scaffold hole — only the banked lemma is the generalized one.)
    bank_failures: list[dict[str, str]] = []
    banked = 0
    for sg_id, win in discharged.items():
        sub = next((s for s in subtasks if s.id == sg_id), None)
        to_bank = ctx.get(_bb_key("abstracted", sg_id)) or win
        store = lemma_bank.store_lemma(
            to_bank.get("statement", "") or sg_id,
            to_bank.get("proof_term", ""),
            source_goal=ctx.request,
            verification_mode="full",
            generality_score=float(to_bank.get("generality_score", 0.0) or 0.0),
            lean_type=(sub.lean_type if sub else None) or to_bank.get("lean_type"),
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
register_native_handler("compare", compare_handler)
register_native_handler("abstract", abstract_handler)
register_native_handler("bank", bank_handler)
