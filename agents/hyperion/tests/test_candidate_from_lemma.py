"""Path-A candidate construction — instance-safe lemma reuse (build plan Priority 1).

The earlier ``_candidate_from_lemma`` pasted a banked lemma's ``proof_term`` verbatim
into ``example : {goal} := {proof_term}``. That is ill-typed whenever the new goal is an
*instance* of a ∀-lemma (e.g. ``example : 0 + 0 = 0 := fun n => Nat.zero_add n``), so
Path A failed to verify even when the lemma genuinely applied — the live snowball never
closed. These hermetic tests pin the new construction: re-prove the lemma as a local
``have h`` of its own type and discharge the goal through ``first | exact h | apply h |
simpa using h`` (the same unification the applicability gate already proves works). The
live-kernel acceptance is the ``@pytest.mark.lean`` snowball; here we assert structure.
"""

from __future__ import annotations

import re

from hyperion.crews.lean_handlers import (
    _candidate_from_lemma,
    _normalize_proof_rhs,
    _prose_to_goal_type,
)


def test_prose_to_goal_type_strips_natural_language_framing():
    # The retrieval query must be the bare Lean type, not English — prose embeds far from
    # the banked lemma types, so an un-stripped request retrieves nothing.
    assert _prose_to_goal_type("Prove that 0 + 7 = 7.") == "0 + 7 = 7"
    assert _prose_to_goal_type("Prove in Lean 4 that n + 0 = n") == "n + 0 = n"
    assert _prose_to_goal_type("Show that 5 = 5") == "5 = 5"
    assert _prose_to_goal_type("Please prove the following: a + b = b + a") == "a + b = b + a"


def test_prose_to_goal_type_is_conservative():
    # Already-bare types pass through; an empty/whitespace request never worsens.
    assert _prose_to_goal_type("0 + 7 = 7") == "0 + 7 = 7"
    assert _prose_to_goal_type("") == ""
    # Nothing to strip -> trimmed original (never empties a real request).
    assert _prose_to_goal_type("  a = a  ") == "a = a"


def test_instance_goal_does_not_paste_proof_term_verbatim():
    # The exact bug: a ∀-lemma reused against an instance goal must NOT become the
    # ill-typed `example : 0 + 0 = 0 := fun n => Nat.zero_add n`.
    lemma = {
        "statement": "∀ (n : Nat), 0 + n = n",
        "proof_term": "fun n => Nat.zero_add n",
        "lean_type": "∀ (n : Nat), 0 + n = n",
    }
    cand = _candidate_from_lemma("0 + 0 = 0", lemma)
    src = cand["source"]
    assert src != "example : 0 + 0 = 0 := fun n => Nat.zero_add n"
    # Lemma re-proved as a local `h` of its OWN type, then applied to the goal.
    assert "example : 0 + 0 = 0 := by" in src
    assert "have h : ∀ (n : Nat), 0 + n = n := fun n => Nat.zero_add n" in src
    assert "first | exact h | apply h | simpa using h" in src


def test_lean_type_falls_back_to_statement_extraction():
    # Live-banked payloads often carry no `lean_type`; the type is extracted from the
    # `statement` (which may be a full decl) so `have h : <type>` is still well-formed.
    lemma = {
        "statement": "theorem nat_refl : ∀ (n : Nat), n = n := fun _ => rfl",
        "proof_term": "fun _ => rfl",
        "lean_type": None,
    }
    cand = _candidate_from_lemma("5 = 5", lemma)
    assert "have h : ∀ (n : Nat), n = n :=" in cand["source"]


def test_origin_and_payload_fields_preserved_for_banking():
    lemma = {
        "statement": "∀ (n : Nat), n = n",
        "proof_term": "rfl",
        "lean_type": "∀ (n : Nat), n = n",
        "origin": "skill_library",
        "source_collection": "skill_library",
    }
    cand = _candidate_from_lemma("7 = 7", lemma)
    assert cand["origin"] == "skill_library"
    assert cand["source_collection"] == "skill_library"
    assert cand["statement"] == "∀ (n : Nat), n = n"
    assert cand["proof_term"] == "rfl"
    assert cand["lean_type"] == "∀ (n : Nat), n = n"


def test_normalize_multiline_tactic_block_collapses_to_one_line():
    # Live runs store tactic blocks; pasted into `have h : T := <block>` a multi-line
    # block breaks on indentation. It must collapse to `by t1; t2`.
    assert _normalize_proof_rhs("\n  intro n\n  rfl") == "by intro n; rfl"
    assert _normalize_proof_rhs("by\n  rfl") == "by rfl"
    # No raw newline survives in the collapsed tactic form.
    assert "\n" not in _normalize_proof_rhs("\n  intro n\n  simp\n  rfl")


def test_normalize_keeps_term_proofs_verbatim():
    assert _normalize_proof_rhs("fun n => Nat.zero_add n") == "fun n => Nat.zero_add n"
    assert _normalize_proof_rhs("Nat.add_comm") == "Nat.add_comm"
    assert _normalize_proof_rhs("rfl") == "rfl"


def test_normalize_empty_degrades_cleanly():
    assert _normalize_proof_rhs("") == "by exact?"
    assert _normalize_proof_rhs(None) == "by exact?"  # type: ignore[arg-type]


def test_no_bare_newline_indentation_hazard_in_have_line():
    # The whole point of normalization: the constructed source must keep the lemma proof
    # on a single `have` line so column-sensitive parsing can't bite.
    lemma = {"statement": "∀ n : Nat, n = n", "proof_term": "\n  intro n\n  rfl", "lean_type": None}
    src = _candidate_from_lemma("3 = 3", lemma)["source"]
    have_line = next(ln for ln in src.splitlines() if ln.strip().startswith("have h"))
    assert "intro n; rfl" in have_line
    # exactly three lines: example header, have, first-combinator
    assert len(src.splitlines()) == 3
    assert re.search(r"have h : .+ := by intro n; rfl", have_line)


# ---------------------------------------------------------------------------
# multi-lemma composition — the depth>=2 candidate (build-plan depth axis)
# ---------------------------------------------------------------------------

from hyperion.crews import lean_handlers
from hyperion.crews.lean_handlers import (
    _compose_multi_source,
    _multi_candidate_from_lemmas,
    _necessary_lemma_ids,
)


def test_compose_multi_source_binds_each_lemma_and_uses_banked_only_closer():
    lemmas = [
        {"id": "a", "lean_type": "∀ n, 0 + n = n", "proof_term": "fun n => Nat.zero_add n"},
        {"id": "b", "lean_type": "∀ a b, a + b = b + a", "proof_term": "Nat.add_comm"},
    ]
    src = _compose_multi_source("∀ a b, 0 + a + b = b + a", lemmas)
    # One `have` per lemma, named h0/h1, each carrying its own type.
    assert "have h0 : ∀ n, 0 + n = n := fun n => Nat.zero_add n" in src
    assert "have h1 : ∀ a b, a + b = b + a := Nat.add_comm" in src
    # Closer composes the banked hypotheses only — no ambient Mathlib normalizers.
    assert "simp only [h0, h1]" in src
    assert "rw [h0, h1]" in src
    assert "add_left_comm" not in src and "add_comm]" not in src


def test_multi_candidate_carries_all_ids_and_payloads_for_ablation():
    lemmas = [{"id": "a", "lean_type": "TA", "proof_term": "pa"},
              {"id": "b", "lean_type": "TB", "proof_term": "pb"}]
    cand = _multi_candidate_from_lemmas("GOAL", lemmas)
    assert cand["multi"] is True
    assert cand["origin"] == "compose"
    assert cand["id"] is None                       # a composition isn't itself a lemma
    assert cand["lemmas_used"] == ["a", "b"]         # offered set (verify ablates this down)
    assert cand["compose_lemmas"] == lemmas          # payloads kept so verify can re-compose


def test_necessary_lemma_ids_credits_only_the_needed_subset(monkeypatch):
    # Goal closes iff BOTH TA and TB are present; TC is dead weight handed to simp.
    lemmas = [{"id": "a", "lean_type": "TA", "proof_term": "pa"},
              {"id": "b", "lean_type": "TB", "proof_term": "pb"},
              {"id": "c", "lean_type": "TC", "proof_term": "pc"}]
    monkeypatch.setattr(
        lean_handlers, "_full_verdict",
        lambda src: (("TA" in src and "TB" in src), []),
    )
    # Single-drop: removing a or b breaks it (necessary); removing c still closes (dead).
    assert set(_necessary_lemma_ids("GOAL", lemmas)) == {"a", "b"}


def test_necessary_lemma_ids_empty_when_no_lemma_is_needed(monkeypatch):
    # The depth-0 case: simp/rfl closes regardless of the banked set ⇒ not a reuse win.
    lemmas = [{"id": "a", "lean_type": "TA", "proof_term": "pa"},
              {"id": "b", "lean_type": "TB", "proof_term": "pb"}]
    monkeypatch.setattr(lean_handlers, "_full_verdict", lambda src: (True, []))
    assert _necessary_lemma_ids("GOAL", lemmas) == []
