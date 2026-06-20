"""Compare — the deterministic preference function + the thesis triple-log schema.

This is the measurement instrument of the experiment (build plan §Phase 5, baseline §3
"build new" / §5 the compare block). It is deliberately **pure**: no I/O, no LLM, no
Lean — just a total, deterministic ordering over verified candidates and the fixed
``(retrieved, synthesized, winner)`` record that the ``compare`` native handler writes
to the blackboard. Keeping it pure is what makes the DoD's "fully-unit-tested function"
achievable and the thesis curve reproducible.

The generality metric (decision d)
-----------------------------------
"Prefer the more general / shorter / more-reusable lemma." With no Lean parser available
offline, generality is a **textual structural proxy**: the number of universally-
quantified binders in the lemma's ``lean_type`` (``∀`` occurrences plus the leading
implicit/explicit binder groups before the top-level ``:``). More binders ⇒ the statement
abstracts over more things ⇒ more reusable. Ties break toward the shorter proof term, then
the shorter statement, then — fully deterministically — toward **Path A** (reuse-first:
a lemma already in the bank beats re-synthesizing an equally-general one).

The triple-log (decision c)
---------------------------
:class:`TripleLog` is a first-class artifact: its schema is fixed and asserted by
``test_compare.py``. ``compare`` writes one per sub-goal to ``triple_log:<sg>`` on the
blackboard (durable in ``context.json``); Post-work's thesis-curve harness globs those.
``synthesized_verified & winner_path == "A"`` over a run history is the snowball signal
(retrieval beating synthesis as the bank fills); ``compared`` flags the rows that were a
genuine A-vs-B contest (both passed the kernel).
"""

from __future__ import annotations

import time
from typing import Any, Optional, TypedDict

# A candidate is the blackboard candidate dict (build plan Phase 4 schema):
# {source, statement, proof_term, origin, lean_type, [path]}.
Candidate = dict[str, Any]


class TripleLog(TypedDict):
    """The ``(retrieved, synthesized, winner)`` measurement record — the thesis dataset.

    One per sub-goal, written to ``triple_log:<sg>``. Treated as a fixed, first-class
    schema (asserted in tests) so the Post-work harness can read run history reliably.

    Attributes:
        subgoal: The sub-goal id this record measures.
        goal_type: The Lean type that was being proved.
        retrieved: The Path-A candidate that was considered (``candidate_a``), or None.
        retrieved_verified: Whether Path A passed the kernel (``verified_a`` is set).
        synthesized: The Path-B candidate that was considered (``candidate_b``), or None.
        synthesized_verified: Whether Path B passed the kernel (``verified_b`` is set).
        winner_path: ``"A"`` | ``"B"`` | None — which path won the compare.
        winner: The chosen candidate dict, or None when neither path verified.
        scores: ``{"a", "b", "winner"}`` generality scores (0.0 when a side is absent).
        compared: True iff BOTH paths verified — a genuine A-vs-B contest.
        reuse_depth: # distinct banked lemmas the *winning* candidate composed — the
            snowball's load-bearing axis. 0 for synthesis/unsolved; 1 for a single applied
            lemma (breadth); >=2 only when a multi-lemma candidate won (depth). A run whose
            depth stays pinned at 1 is "retrieval fired again", not "the bank compounded".
        mode: ``"research"`` (verify both) | ``"deploy"`` (exploit-first).
        ts: Epoch seconds the record was written.
    """

    subgoal: str
    goal_type: str
    retrieved: Optional[Candidate]
    retrieved_verified: bool
    synthesized: Optional[Candidate]
    synthesized_verified: bool
    winner_path: Optional[str]
    winner: Optional[Candidate]
    scores: dict[str, float]
    compared: bool
    reuse_depth: int
    mode: str
    ts: int


def _lean_type(c: Candidate) -> str:
    """The candidate's bare proposition for metric purposes (``lean_type``, else statement)."""
    return (c.get("lean_type") or c.get("statement") or "").strip()


def _proof_len(c: Candidate) -> int:
    """Length (chars) of the candidate's proof term — shorter is more reusable."""
    return len((c.get("proof_term") or c.get("source") or "").strip())


def _stmt_len(c: Candidate) -> int:
    """Length (chars) of the candidate's statement — the final size tie-break."""
    return len((c.get("statement") or c.get("lean_type") or "").strip())


def generality_score(c: Candidate) -> float:
    """How general / reusable a candidate is — higher wins (decision d).

    Textual structural proxy (no Lean parser offline): the count of universally-
    quantified binders in the ``lean_type`` — every ``∀``/``Π`` quantifier plus each
    leading implicit/explicit binder *group* (``{…}`` / ``[…]`` / ``(…)``) that appears
    before the top-level ``:`` of the proposition. A statement that abstracts over more
    variables is more general, hence more reusable as a banked lemma.

    Returns a float so it maps straight onto ``store_lemma(generality_score=…)``.
    """
    t = _lean_type(c)
    if not t:
        return 0.0
    score = t.count("∀") + t.count("Π")

    # Leading binder groups before the top-level ':' (e.g. "lemma f {α} (a : α) : …").
    # Strip a leading decl keyword + name so we scan the binder region, not the body.
    head = t
    for kw in ("theorem", "lemma", "example", "def", "instance"):
        if head.startswith(kw):
            head = head[len(kw):].lstrip()
            # drop the declaration name token, if any
            parts = head.split(None, 1)
            head = parts[1] if len(parts) == 2 else ""
            break

    depth = 0
    for ch in head:
        if ch in "({[":
            if depth == 0:
                score += 1
            depth += 1
        elif ch in ")}]":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            break  # reached the top-level ':' — past the binder region
    return float(score)


def reuse_depth(winner: Optional[Candidate]) -> int:
    """How many distinct banked lemmas the winning candidate composed (0 for synthesis).

    This is the axis that separates *breadth* (many goals each reusing one lemma — depth
    pinned at 1) from *depth* (one goal composing several banked lemmas — the snowball
    compounding). Only a Path-A winner can have depth; a synthesized or unsolved goal is 0.
    The count reads the candidate's ``lemmas_used`` provenance (the banked-lemma ids it
    introduced as ``have`` hypotheses); a legacy single-lemma candidate that predates that
    field still counts as 1 when it carries an ``id``.
    """
    if not winner or winner.get("path") != "A":
        return 0
    used = winner.get("lemmas_used")
    if used:
        return len({u for u in used if u})
    return 1 if winner.get("id") else 0


def _ordering_key(c: Candidate) -> tuple[float, int, int]:
    """Deterministic total order: more general, then shorter proof, then shorter statement.

    ``max`` over this key selects the winner. The negations make "shorter" rank higher.
    """
    return (generality_score(c), -_proof_len(c), -_stmt_len(c))


def choose_winner(
    verified_a: Optional[Candidate],
    verified_b: Optional[Candidate],
) -> Optional[Candidate]:
    """Pick the preferred verified candidate (decision d). Pure and deterministic.

    Both args are candidates that ALREADY passed the kernel (or None when their path did
    not verify). Returns the more-general / shorter one; with only one verified it is the
    winner; with neither, None.

    Final tie-break (identical ordering keys): prefer **Path A** — reuse-first. A lemma
    already in the bank beats an equally-general freshly-synthesized one. The returned
    dict carries a ``"path"`` key so callers know which won.
    """
    if verified_a is None and verified_b is None:
        return None
    if verified_b is None:
        return {**verified_a, "path": verified_a.get("path", "A")}
    if verified_a is None:
        return {**verified_b, "path": verified_b.get("path", "B")}

    key_a, key_b = _ordering_key(verified_a), _ordering_key(verified_b)
    if key_b > key_a:
        return {**verified_b, "path": verified_b.get("path", "B")}
    # key_a > key_b OR a genuine tie → Path A wins (reuse-first, deterministic).
    return {**verified_a, "path": verified_a.get("path", "A")}


def build_triple(
    *,
    subgoal: str,
    goal_type: str,
    retrieved: Optional[Candidate],
    synthesized: Optional[Candidate],
    verified_a: Optional[Candidate],
    verified_b: Optional[Candidate],
    winner: Optional[Candidate],
    mode: str,
    ts: Optional[int] = None,
) -> TripleLog:
    """Assemble the fixed :class:`TripleLog` record. Pure (``ts`` defaults to now).

    ``retrieved``/``synthesized`` are the candidates that were *considered* (the raw
    Path-A/Path-B outputs); ``verified_a``/``verified_b`` are whether each *passed*. The
    winner's score is reported under ``scores["winner"]`` so the harness needn't recompute.
    """
    score_a = generality_score(verified_a) if verified_a else 0.0
    score_b = generality_score(verified_b) if verified_b else 0.0
    winner_score = generality_score(winner) if winner else 0.0
    return TripleLog(
        subgoal=subgoal,
        goal_type=goal_type,
        retrieved=retrieved,
        retrieved_verified=verified_a is not None,
        synthesized=synthesized,
        synthesized_verified=verified_b is not None,
        winner_path=(winner.get("path") if winner else None),
        winner=winner,
        scores={"a": score_a, "b": score_b, "winner": winner_score},
        compared=(verified_a is not None and verified_b is not None),
        reuse_depth=reuse_depth(winner),
        mode=mode,
        ts=ts if ts is not None else int(time.time()),
    )
