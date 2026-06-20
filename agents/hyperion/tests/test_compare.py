"""Compare — the pure, deterministic preference function + the thesis triple-log schema.

Build plan §Phase 5 Test Gate: "Compare is a pure, fully-unit-tested function;
more-general/shorter wins; ties deterministic; triple-log schema fixed and asserted."

Everything here is offline and LLM/Lean/Qdrant-free — ``lemma_compare`` is pure by
construction (no I/O), which is exactly why these are plain unit tests.
"""

from __future__ import annotations

from hyperion.crews.lemma_compare import (
    TripleLog,
    build_triple,
    choose_winner,
    generality_score,
)

# ---------------------------------------------------------------------------
# generality_score — the structural proxy (more binders = more general)
# ---------------------------------------------------------------------------


def test_generality_counts_forall_binders():
    assert generality_score({"lean_type": "∀ n : Nat, n + 0 = n"}) == 1.0
    assert generality_score({"lean_type": "∀ a b : Nat, a + b = b + a"}) == 1.0  # one ∀ token
    assert generality_score({"lean_type": "P ∧ Q"}) == 0.0
    assert generality_score({"lean_type": "P"}) == 0.0


def test_generality_counts_leading_binder_groups():
    # theorem with one implicit + one explicit binder group before the top-level ':'.
    score = generality_score(
        {"lean_type": "theorem foo {α : Type} (a : α) : a = a"}
    )
    assert score == 2.0


def test_generality_ignores_binders_after_top_level_colon():
    # Groups inside the proposition body (after the top-level ':') do NOT count.
    score = generality_score({"lean_type": "theorem foo (a : Nat) : (a, a) = (a, a)"})
    assert score == 1.0  # only the leading (a : Nat) group


def test_generality_falls_back_to_statement_then_zero():
    assert generality_score({"statement": "∀ x, x = x"}) == 1.0
    assert generality_score({}) == 0.0


# ---------------------------------------------------------------------------
# choose_winner — more general, then shorter, then reuse-first (deterministic)
# ---------------------------------------------------------------------------


def test_more_general_lemma_wins():
    a = {"lean_type": "∀ n : Nat, P n", "proof_term": "by simp", "path": "A"}
    b = {"lean_type": "P 0", "proof_term": "rfl", "path": "B"}
    win = choose_winner(a, b)
    assert win["path"] == "A"
    assert win["lean_type"] == "∀ n : Nat, P n"


def test_tie_on_generality_shorter_proof_wins():
    # Equal generality (both 0 binders) → the shorter proof term is more reusable.
    a = {"lean_type": "P", "proof_term": "by long_tactic_chain", "path": "A"}
    b = {"lean_type": "P", "proof_term": "rfl", "path": "B"}
    assert choose_winner(a, b)["path"] == "B"


def test_full_tie_breaks_to_path_a_reuse_first():
    # Identical ordering keys (same generality, proof len, stmt len) → Path A wins.
    a = {"lean_type": "P", "statement": "lemA", "proof_term": "xxx", "path": "A"}
    b = {"lean_type": "P", "statement": "lemB", "proof_term": "yyy", "path": "B"}
    assert choose_winner(a, b)["path"] == "A"


def test_single_verified_candidate_is_the_winner():
    b = {"lean_type": "P", "proof_term": "rfl"}
    win = choose_winner(None, b)
    assert win["path"] == "B"
    assert choose_winner({"lean_type": "Q", "proof_term": "trivial"}, None)["path"] == "A"


def test_no_verified_candidate_is_none():
    assert choose_winner(None, None) is None


def test_choose_winner_does_not_mutate_inputs():
    a = {"lean_type": "∀ n, P n", "proof_term": "by simp"}
    b = {"lean_type": "P 0", "proof_term": "rfl"}
    choose_winner(a, b)
    assert "path" not in a and "path" not in b  # winner is a copy


def test_choose_winner_is_deterministic_under_arg_swap():
    # A genuine tie must resolve to Path A regardless of which arg is "a"/"b" structurally;
    # the function is keyed on the explicit path field, so the result is stable.
    a = {"lean_type": "P", "proof_term": "xxx", "path": "A"}
    b = {"lean_type": "P", "proof_term": "yyy", "path": "B"}
    assert choose_winner(a, b)["path"] == "A"
    assert choose_winner(a, b)["path"] == "A"  # repeatable


# ---------------------------------------------------------------------------
# build_triple — the fixed thesis-dataset schema
# ---------------------------------------------------------------------------

_RETRIEVED = {"lean_type": "∀ n, P n", "proof_term": "by simp", "statement": "lemP", "path": "A",
              "lemmas_used": ["lem-1"]}
_SYNTH = {"lean_type": "P 0", "proof_term": "rfl", "statement": "synthP", "path": "B"}

_SCHEMA_KEYS = set(TripleLog.__annotations__)


def test_triple_schema_is_fixed_and_complete():
    triple = build_triple(
        subgoal="h1", goal_type="P 0",
        retrieved=_RETRIEVED, synthesized=_SYNTH,
        verified_a=_RETRIEVED, verified_b=_SYNTH,
        winner={**_RETRIEVED}, mode="research", ts=123,
    )
    # Every declared field present, and exactly those (schema is a first-class artifact).
    assert set(triple.keys()) == _SCHEMA_KEYS
    assert triple["subgoal"] == "h1"
    assert triple["goal_type"] == "P 0"
    assert triple["retrieved_verified"] is True
    assert triple["synthesized_verified"] is True
    assert triple["compared"] is True          # both paths verified ⇒ genuine contest
    assert triple["winner_path"] == "A"
    assert triple["mode"] == "research"
    assert triple["ts"] == 123
    assert triple["scores"]["a"] == 1.0
    assert triple["scores"]["b"] == 0.0
    assert triple["scores"]["winner"] == 1.0
    assert triple["reuse_depth"] == 1          # Path-A winner composed one banked lemma


def test_triple_marks_uncompared_when_only_one_path_verified():
    triple = build_triple(
        subgoal="h2", goal_type="Q",
        retrieved=None, synthesized=_SYNTH,
        verified_a=None, verified_b=_SYNTH,
        winner={**_SYNTH}, mode="deploy", ts=1,
    )
    assert triple["compared"] is False         # only Path B verified
    assert triple["retrieved_verified"] is False
    assert triple["synthesized_verified"] is True
    assert triple["winner_path"] == "B"
    assert triple["reuse_depth"] == 0          # synthesis win has no reuse depth


def test_triple_records_a_failed_subgoal_shape():
    # Defensive: build_triple tolerates a None winner (no path verified).
    triple = build_triple(
        subgoal="h3", goal_type="R",
        retrieved=_RETRIEVED, synthesized=None,
        verified_a=None, verified_b=None,
        winner=None, mode="deploy", ts=1,
    )
    assert triple["winner"] is None
    assert triple["winner_path"] is None
    assert triple["scores"]["winner"] == 0.0
    assert triple["compared"] is False
    assert triple["reuse_depth"] == 0          # unsolved ⇒ no reuse


# ---------------------------------------------------------------------------
# reuse_depth — the breadth-vs-depth axis
# ---------------------------------------------------------------------------


def test_triple_records_weak_gate_counterfactual():
    # Weak gate: a full-strength Path B closed the goal (b_strong) but no *eligible* weak proof
    # did (verified_b None), so Path A carried it — the bank was necessary under a weak prover.
    triple = build_triple(
        subgoal="h1", goal_type="0 + n = n",
        retrieved=_RETRIEVED, synthesized=_SYNTH,
        verified_a=_RETRIEVED, verified_b=None,
        verified_b_strong=_SYNTH,                  # strong prover would have solved it
        winner={**_RETRIEVED}, mode="research", ts=1,
    )
    assert triple["winner_path"] == "A"
    assert triple["synthesized_verified"] is False        # no eligible (weak) Path B
    assert triple["synthesized_verified_strong"] is True  # but a strong prover could
    assert triple["path_b_gated"] is True                 # the gate forced Path A to carry it


def test_triple_strong_defaults_to_eligible_when_gate_off():
    # No counterfactual passed ⇒ strong == eligible (gate off, historical behavior).
    triple = build_triple(
        subgoal="h2", goal_type="Q",
        retrieved=None, synthesized=_SYNTH,
        verified_a=None, verified_b=_SYNTH,
        winner={**_SYNTH}, mode="deploy", ts=1,
    )
    assert triple["synthesized_verified_strong"] is True
    assert triple["path_b_gated"] is False


def test_reuse_depth_zero_for_synthesis_and_none():
    from hyperion.crews.lemma_compare import reuse_depth
    assert reuse_depth(None) == 0
    assert reuse_depth({"path": "B", "lemmas_used": ["x"]}) == 0   # only Path A reuses


def test_reuse_depth_counts_distinct_banked_lemmas():
    from hyperion.crews.lemma_compare import reuse_depth
    # Breadth: one applied lemma.
    assert reuse_depth({"path": "A", "lemmas_used": ["a"]}) == 1
    # Depth: a multi-lemma candidate composed three distinct banked lemmas.
    assert reuse_depth({"path": "A", "lemmas_used": ["a", "b", "c"]}) == 3
    # Distinct, not raw count — a repeated id is one lemma.
    assert reuse_depth({"path": "A", "lemmas_used": ["a", "a", "b"]}) == 2
    # Legacy single-lemma candidate (no lemmas_used) still counts as 1 via its id.
    assert reuse_depth({"path": "A", "id": "a"}) == 1
