"""Applicability-aware lemma retrieval — the Path-A sourcing step (build plan Phase 3).

Role in the system
------------------
This is *exploit* (Path A): given a sub-goal, pull verified lemmas the prover has
already banked instead of re-synthesizing one (Path B). It is the precision front-end
to the lemma bank — embed the goal → vector search (Phase 2 ``lemma_bank``) → rerank →
**applicability gate** — and it hands the verify controller (Phase 4) a short, ranked
list of lemmas that plausibly *apply*, not merely ones that read similarly.

Why a gate on top of rerank (baseline risk #2)
----------------------------------------------
Vector search and the cross-encoder reranker both measure *textual* relevance. But a
lemma being worded like the goal does not mean it **unifies** with the goal. So after
the coarse rerank we add a cheap Lean-aware precision pass: for each reranked
candidate, ask the kernel whether ``exact``/``apply`` the lemma makes progress on the
goal. Candidates that rerank well but don't apply are dropped; the survivors stay in
ranked order.

Pipeline (over-fetch → rerank → gate → budget-trim), reusing the shape of
``tools/second_brain.py`` and the primitives in ``tools/reranker.py``; the *source* is
``lemma_bank`` (Phase 2), not the personal second brain.

The load-bearing ``infra_ok`` distinction (mirrors ``tools/lean_verify.py``)
---------------------------------------------------------------------------
The gate routes on the verifier's ``infra_ok`` flag FIRST, exactly like every other
verifier caller:

  - ``infra_ok=False`` (verifier unreachable) → **KEEP** the candidate. A probe we
    could not run is *inconclusive*, never a drop — degrade to rerank order rather
    than silently discarding a possibly-applicable lemma because the sidecar blinked.
  - ``ok=True``  → KEEP (the lemma unifies → applicable).
  - ``ok=False`` → DROP (reranked well but does not apply).

Every external dependency is fail-soft: an empty/failed bank read yields ``[]``, a
reranker outage degrades to vector order, and a verifier outage degrades to "keep all".
"""

from __future__ import annotations

import logging
import re
from typing import Any

from crewai.tools import BaseTool

from hyperion.memory import lemma_bank
from hyperion.tools.lean_verify import verify_lean
from hyperion.tools.reranker import _estimate_tokens, rerank

logger = logging.getLogger(__name__)

# Decl keywords whose signature we strip when extracting a bare type for the probe.
_DECL_RE = re.compile(r"^\s*(?:theorem|lemma|def|example|instance|abbrev)\b")

# Brackets whose depth we track so a binder colon (e.g. ``(n : Nat)``) is not mistaken
# for the signature colon that separates the binders from the goal type.
_OPENERS = {"(": ")", "{": "}", "[": "]", "⟨": "⟩"}
_CLOSERS = {")", "}", "]", "⟩"}


def _first_top_level(text: str, needles: tuple[str, ...]) -> int:
    """Index of the first occurrence of any ``needle`` at bracket-depth 0, else -1.

    A tiny depth scanner over ``()``/``{}``/``[]``/``⟨⟩`` so we split a Lean signature
    on its *real* separator, not on punctuation buried inside a binder. ``needles`` are
    matched as literal substrings (the colon ``":"`` and the assignment ``":="``).
    """
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
        elif depth == 0:
            for needle in needles:
                if text.startswith(needle, i):
                    return i
        i += 1
    return -1


def _lemma_type(statement: str) -> str:
    """Best-effort extraction of a lemma's *type* from its stored ``statement``.

    Phase 2 banks the full ``"theorem NAME : TYPE := proof"`` string, not the bare
    type. The probe needs the type alone (to inline it as a hypothesis), so we:

      1. strip a leading decl keyword + name + binders up to the first **top-level**
         ``:`` (depth-scanned, so ``(n : Nat)`` binders don't trip the split), then
      2. strip any top-level ``:=`` / ``where`` proof suffix.

    Falls back to the whole (stripped) statement if extraction yields nothing — a
    degraded probe is still a valid probe, and the offline gate never depends on the
    exact string.

    TRADEOFF (flagged, build plan Phase 3 step 2b): this is a dependency-free
    heuristic and will mishandle exotic signatures. The robust fix is to store the
    bare type as a first-class ``lemma_bank`` payload field — a Phase 2 schema change
    deferred until the Phase 4 / live-Lean wiring needs it.
    """
    s = statement.strip()

    # 1. Strip the decl keyword + name + binders, up to the signature colon.
    if _DECL_RE.match(s):
        colon = _first_top_level(s, (":",))
        if colon != -1:
            s = s[colon + 1 :]

    # 2. Strip the proof / definition suffix (":= ..." or "where ...").
    cut = _first_top_level(s, (":=",))
    if cut != -1:
        s = s[:cut]
    where = _first_top_level(s, ("where",))
    if where != -1:
        s = s[:where]

    s = s.strip()
    return s or statement.strip()


def _probe_source(goal: str, statement: str) -> str:
    """Build the self-contained applicability probe for ``statement`` against ``goal``.

    Inlines the lemma's *type* as a local hypothesis ``h`` and asks the kernel whether
    it discharges the goal directly (``exact h``) or makes progress (``apply h`` leaving
    subgoals). Self-contained on purpose: ``h`` is a hypothesis, so the probe needs no
    Mathlib name resolution. Verified in ``skeleton`` mode so an ``apply`` that unifies
    but leaves subgoals (closed here by ``sorry``) still counts as "makes progress".
    """
    lemma_type = _lemma_type(statement)
    goal_type = _lemma_type(goal)
    return (
        f"example (h : {lemma_type}) : {goal_type} := by\n"
        f"  first | exact h | (apply h; all_goals sorry)"
    )


def _applies(goal: str, statement: str) -> bool:
    """Run the cheap Lean probe and route on the load-bearing ``infra_ok`` flag.

    Returns ``True`` (KEEP) when the lemma unifies *or* when the probe is
    inconclusive because the verifier is down — an un-runnable probe must never drop a
    candidate (mirrors the verifier-down posture in ``tools/lean_verify.py``). Returns
    ``False`` (DROP) only on a real ``ok=False`` verdict: reranked well, does not apply.
    """
    res = verify_lean(_probe_source(goal, statement), mode="skeleton")
    if not res["infra_ok"]:
        # Inconclusive ≠ drop: degrade to rerank order, never discard on infra failure.
        return True
    return bool(res["ok"])


def retrieve_applicable_lemmas(
    goal: str,
    *,
    limit: int = 5,
    over_fetch: int = 15,
    token_budget: int | None = None,
    probe: bool = True,
) -> list[dict[str, Any]]:
    """Retrieve banked lemmas that plausibly *apply* to ``goal``, in ranked order.

    The Path-A pipeline: over-fetch from the lemma bank by vector similarity, rerank by
    textual relevance, then (the precision pass) drop candidates that don't unify with
    the goal, and finally trim to a token budget / ``limit``.

    Args:
        goal: The current sub-goal / goal type to source lemmas for. Used as the
            embedding + rerank query, and (via :func:`_lemma_type`) as the probe goal.
        limit: Maximum lemmas to return after gating + trimming.
        over_fetch: How many candidates to pull from the bank before reranking — a
            wider net so the rerank + applicability gate have room to work.
        token_budget: Optional approximate token ceiling on the kept set (applied after
            the gate, lowest-ranked dropped first; at least one survivor is always kept).
        probe: When ``False``, skip the applicability gate entirely and return rerank
            order (``verify_lean`` is never called). The escape hatch for callers that
            want pure textual ranking, or when no verifier is wired.

    Returns:
        Lemma payload dicts (the :func:`lemma_bank.retrieve_lemmas` shape, incl.
        ``statement``/``proof_term``/``score``) for the applying lemmas, best-first.
        Empty when the bank is empty or every dependency degraded to nothing.
    """
    candidates = lemma_bank.retrieve_lemmas(goal, limit=over_fetch)
    if not candidates:
        return []

    # Coarse precision pass: rerank by goal-vs-statement text. Fail-soft — a reranker
    # outage returns the candidates in their original (vector) order.
    statements = [c["statement"] for c in candidates]
    ranked = rerank(goal, statements, top_n=len(statements))
    reranked = [candidates[idx] for idx, _score in ranked]

    # Applicability gate (the Phase 3 contribution): keep appliers + infra-down
    # inconclusives, drop reranked-but-non-applying lemmas. Probed in ranked order.
    if probe:
        reranked = [c for c in reranked if _applies(goal, c["statement"])]

    # Token-budget trim (mirrors reranker.prioritize): keep best-first until the next
    # candidate would overflow, but always keep at least one. Then cap at ``limit``.
    kept: list[dict[str, Any]] = []
    used = 0
    dropped = 0
    for cand in reranked:
        if len(kept) >= limit:
            break
        if token_budget is not None:
            cost = _estimate_tokens(cand["statement"])
            if used + cost > token_budget and kept:
                dropped += 1
                continue
            used += cost
        kept.append(cand)
    if dropped:
        logger.info(
            "retrieve_applicable_lemmas: trimmed %d candidate(s) to fit token_budget=%s",
            dropped, token_budget,
        )
    return kept


class LemmaRetrievalTool(BaseTool):
    """CrewAI tool wrapper around :func:`retrieve_applicable_lemmas` (optional surface).

    Native nodes (Phase 4 ``retrieve``) call the plain function directly — no ReAct
    loop. This wrapper exists for parity with :class:`LeanVerifyTool` /
    :class:`SecondBrainTool` so an agent could pull applicable lemmas as a tool call;
    it renders the verdicts as a compact, LLM-readable string.
    """

    name: str = "retrieve_applicable_lemmas"
    description: str = (
        "Retrieve previously-verified Lean lemmas that apply to a goal. "
        "Input: 'goal' (the Lean goal type). Returns ranked applicable lemmas "
        "(statement + proof term), filtered so textually-similar but non-applying "
        "lemmas are dropped."
    )

    def _run(self, goal: str) -> str:
        lemmas = retrieve_applicable_lemmas(goal)
        if not lemmas:
            return "(No applicable lemmas found in the bank.)"
        lines = [f"## Applicable lemmas for: {goal!r}\n"]
        for lemma in lemmas:
            lines.append(f"### {lemma['statement']} (score: {lemma.get('score', 0.0)})")
            if lemma.get("proof_term"):
                lines.append(f"proof: {lemma['proof_term']}")
            lines.append("")
        return "\n".join(lines)
