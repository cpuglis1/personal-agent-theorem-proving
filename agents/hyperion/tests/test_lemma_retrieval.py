"""Tests for applicability-aware lemma retrieval (build-plan Phase 3).

Unit tier, fully offline: the bank read is mocked by patching
``lemma_bank.retrieve_lemmas``, the reranker is exercised real-but-degraded (its
``httpx.post`` patched) or patched directly to control order, and ``verify_lean`` is
replaced by content-aware fakes / ``mock_lean`` — no Qdrant, no Infinity, no Lean.

The Phase 3 Test Gate (build plan §Phase 3):
  - A crafted case where the top *textual* (reranked-first) match does NOT apply and a
    lower one does → ``retrieve_applicable_lemmas`` returns the APPLYING lemma first,
    the non-applier dropped. (The core correctness claim — risk #2.)
  - Reranker mocked DOWN → retrieval still returns vector-ordered candidates (fail-soft).
  - Probe infra DOWN (``infra_ok=False``) → NO candidate dropped (inconclusive ≠ drop) —
    the load-bearing distinction, asserted.
  - Token-budget trim preserved; empty bank → ``[]``; ``probe=False`` skips the gate
    (returns rerank order, ``verify_lean`` not called).
  - Runs fully offline.
"""

from __future__ import annotations

from unittest.mock import patch

from hyperion.tools import lemma_retrieval

from lean_mock import mock_lean

_PROBE_TARGET = ("hyperion.tools.lemma_retrieval.verify_lean",)


def _lemma(statement: str, score: float, proof_term: str = "by sorry") -> dict:
    """A lemma payload in the ``lemma_bank.retrieve_lemmas`` shape."""
    lean_type = lemma_retrieval._lemma_type(statement)
    return {
        "statement": statement,
        "lean_type": lean_type,
        "proof_term": proof_term,
        "origin": "skill_library",
        "source_collection": "skill_library",
        "normalized_key": statement,
        "symbol_set": lemma_retrieval.lemma_bank.symbol_set(lean_type),
        "times_retrieved": 0,
        "times_won": 0,
        "generality_score": 0.0,
        "source_goal": "",
        "verified_at": 1,
        "verification_mode": "full",
        "score": score,
    }


def _rerank_identity(query, documents, top_n=5):
    """A reranker stand-in that preserves input (vector) order — like a degraded run."""
    return [(i, 0.0) for i in range(min(top_n, len(documents)))]


def _rerank_order(order):
    """A reranker stand-in that returns a fixed permutation of input indices."""

    def _fake(query, documents, top_n=5):
        return [(i, float(len(order) - rank)) for rank, i in enumerate(order)][:top_n]

    return _fake


# ---------------------------------------------------------------------------
# THE CORE CLAIM — applicability gate keeps the applier, drops the non-applier
# ---------------------------------------------------------------------------


def test_applying_lemma_returned_first_non_applier_dropped():
    """Top *textual* (reranked-first) match does NOT apply; a lower one does.

    Retrieval must return the APPLYING lemma first and DROP the textually-similar
    non-applier — textual relevance ≠ logical applicability (risk #2).
    """
    non_applier = "theorem nope : a = a := rfl"
    applier = "theorem yes : a + b = b + a := by ring"

    bank = [_lemma(applier, 0.80), _lemma(non_applier, 0.70)]

    # Reranker puts the NON-applier first (it reads more like the goal). The gate must
    # still demote it below the applier.
    def fake_rerank(query, documents, top_n=5):
        # index 1 (non_applier) ranked first, index 0 (applier) second.
        return [(1, 0.99), (0, 0.50)][:top_n]

    # Content-aware: ok=False only when the probe inlines the non-applying lemma's type.
    def content_aware(source, *, mode="full", timeout=None):
        applies = "a = a" not in source  # the non_applier's distinctive type
        return {"ok": applies, "errors": [], "elaborated_term": None,
                "mode": mode, "infra_ok": True}

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(lemma_retrieval, "rerank", side_effect=fake_rerank), \
         patch.object(lemma_retrieval, "verify_lean", side_effect=content_aware):
        out = lemma_retrieval.retrieve_applicable_lemmas("a + b = b + a")

    assert [r["statement"] for r in out] == [applier]  # applier kept, first; non-applier gone


# ---------------------------------------------------------------------------
# Reranker DOWN → vector-ordered candidates still returned (fail-soft)
# ---------------------------------------------------------------------------


def test_reranker_down_degrades_to_vector_order():
    """A reranker outage must not break retrieval — degrade to the bank's vector order."""
    bank = [_lemma("theorem a : A := pa", 0.90), _lemma("theorem b : B := pb", 0.50)]

    # Exercise the real reranker fail-soft path: its httpx.post raises → original order.
    import hyperion.tools.reranker as reranker

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(reranker.httpx, "post", side_effect=Exception("infinity down")), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal")

    # Vector order (bank order) preserved despite the reranker being unreachable.
    assert [r["statement"] for r in out] == ["theorem a : A := pa", "theorem b : B := pb"]


# ---------------------------------------------------------------------------
# Probe infra DOWN → NO candidate dropped (inconclusive ≠ drop) — load-bearing
# ---------------------------------------------------------------------------


def test_probe_infra_down_keeps_all_candidates():
    """When the verifier is unreachable the probe is inconclusive — KEEP every
    candidate, never drop on infra failure (mirrors lean_verify's infra_ok posture)."""
    bank = [_lemma("theorem a : A := pa", 0.90), _lemma("theorem b : B := pb", 0.50)]

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=False, infra_ok=False, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal")

    # infra_ok=False AND ok=False, yet nothing is dropped — inconclusive never drops.
    assert [r["statement"] for r in out] == ["theorem a : A := pa", "theorem b : B := pb"]


# ---------------------------------------------------------------------------
# probe=False skips the gate entirely (verify_lean never called)
# ---------------------------------------------------------------------------


def test_probe_false_skips_gate_and_never_calls_verifier():
    bank = [_lemma("theorem a : A := pa", 0.90), _lemma("theorem b : B := pb", 0.50)]

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=False, targets=_PROBE_TARGET) as lean:
        out = lemma_retrieval.retrieve_applicable_lemmas("goal", probe=False)

    assert [r["statement"] for r in out] == ["theorem a : A := pa", "theorem b : B := pb"]
    lean.assert_not_called()  # gate skipped → the oracle is never consulted


# ---------------------------------------------------------------------------
# Token-budget trim preserved (after the gate, lowest-ranked dropped first)
# ---------------------------------------------------------------------------


def test_token_budget_trims_lowest_ranked_keeping_at_least_one():
    # Two ~equal-length statements (~10 tokens each at ≈4 chars/token); a tiny budget
    # admits the first but not the second.
    s1 = "theorem one : aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa := p1"
    s2 = "theorem two : bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb := p2"
    bank = [_lemma(s1, 0.90), _lemma(s2, 0.80)]

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal", token_budget=10)

    assert [r["statement"] for r in out] == [s1]  # second trimmed to fit the budget


def test_token_budget_keeps_first_even_if_it_alone_overflows():
    """Better to return something than nothing: the top candidate is kept even if it
    alone exceeds the budget (mirrors reranker.prioritize's 'and kept' guard)."""
    big = "theorem big : " + "x" * 400 + " := p"
    bank = [_lemma(big, 0.90)]

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal", token_budget=1)

    assert [r["statement"] for r in out] == [big]


def test_limit_caps_returned_count():
    bank = [_lemma(f"theorem t{i} : G{i} := p{i}", 0.9 - i / 100) for i in range(6)]
    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal", limit=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Empty bank → [] (and the reranker / verifier are never consulted)
# ---------------------------------------------------------------------------


def test_empty_bank_returns_empty():
    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=[]), \
         patch.object(lemma_retrieval, "rerank", side_effect=AssertionError("should not rerank")), \
         mock_lean(ok=True, targets=_PROBE_TARGET) as lean:
        out = lemma_retrieval.retrieve_applicable_lemmas("goal")
    assert out == []
    lean.assert_not_called()


def test_default_retrieval_mode_reads_skill_library_only():
    skill = [_lemma("theorem skill : S := ps", 0.90)]
    with patch.object(lemma_retrieval.settings, "lemma_retrieval_mode", "skill"), \
         patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=skill) as retrieve_skill, \
         patch.object(lemma_retrieval.lemma_bank, "retrieve_mathlib_premises", return_value=[]) as retrieve_mathlib, \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal")

    assert [r["statement"] for r in out] == ["theorem skill : S := ps"]
    retrieve_skill.assert_called_once()
    retrieve_mathlib.assert_not_called()


def test_combined_retrieval_mode_reads_both_sources():
    skill = [_lemma("theorem skill : S := ps", 0.90)]
    mathlib = [_lemma("theorem mathlib : M := pm", 0.80)]
    mathlib[0]["origin"] = "mathlib"
    mathlib[0]["source_collection"] = "mathlib_premises"

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=skill), \
         patch.object(lemma_retrieval.lemma_bank, "retrieve_mathlib_premises", return_value=mathlib), \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal", mode="combined")

    assert {r["origin"] for r in out} == {"skill_library", "mathlib"}


# ---------------------------------------------------------------------------
# Over-fetch is honored; rerank order drives the result order
# ---------------------------------------------------------------------------


def test_over_fetch_passed_to_bank_and_rerank_order_honored():
    bank = [_lemma("theorem a : A := pa", 0.90),
            _lemma("theorem b : B := pb", 0.50),
            _lemma("theorem c : C := pc", 0.10)]

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank) as recall, \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_order([2, 0, 1])), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("goal", over_fetch=15)

    assert recall.call_args.kwargs["limit"] == 15  # over-fetch wired through to the bank
    assert [r["statement"] for r in out] == [
        "theorem c : C := pc", "theorem a : A := pa", "theorem b : B := pb",
    ]


def test_symbol_fusion_promotes_exact_lean_type_match_before_rerank():
    dense_only = _lemma("theorem dense : DenseOnly := p", 0.99)
    symbol_match = _lemma("theorem add_zero : ∀ (n : Nat), n + 0 = n := p", 0.80)
    tail = _lemma("theorem tail : TailOnly := p", 0.10)
    bank = [dense_only, symbol_match, tail]

    with patch.object(lemma_retrieval.lemma_bank, "retrieve_lemmas", return_value=bank), \
         patch.object(lemma_retrieval, "rerank", side_effect=_rerank_identity), \
         mock_lean(ok=True, targets=_PROBE_TARGET):
        out = lemma_retrieval.retrieve_applicable_lemmas("0 + 0 = 0")

    assert out[0]["statement"] == symbol_match["statement"]


# ---------------------------------------------------------------------------
# The probe is self-contained (inlines the lemma TYPE as a hypothesis)
# ---------------------------------------------------------------------------


def test_probe_source_inlines_lemma_type_as_hypothesis():
    src = lemma_retrieval._probe_source("a + b = b + a", "theorem add_comm : a + b = b + a := by ring")
    # The lemma's TYPE (not name, not proof) is inlined as hypothesis h, against the goal.
    assert "example (h : a + b = b + a) : a + b = b + a := by" in src
    assert "exact h" in src and "apply h" in src
    assert "add_comm" not in src  # name-free → no Mathlib resolution needed
    assert ":= by ring" not in src  # proof stripped


def test_lemma_type_extraction_handles_binders():
    # A binder colon (inside `(n : Nat)`) must NOT be mistaken for the signature colon:
    # the split happens at the top-level `:` after the binders, yielding the bare
    # proposition. (Stripping binders to a bare prop is the flagged heuristic limitation;
    # the robust fix is storing the type as a first-class field — deferred to Phase 2.)
    t = lemma_retrieval._lemma_type("theorem foo (n : Nat) : n + 0 = n := by simp")
    assert t == "n + 0 = n"


def test_lemma_type_falls_back_to_whole_statement():
    # A bare type with no decl keyword / no proof suffix passes through unchanged.
    assert lemma_retrieval._lemma_type("a + b = b + a") == "a + b = b + a"
