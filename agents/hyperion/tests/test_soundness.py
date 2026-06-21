"""Tests for the soundness contract (PLAN-definition-synthesis §Soundness contract).

The contract is the non-negotiable ``sorryAx`` gate: a proof counts as solved only if
its ``#print axioms`` set is within the standard sound base, with no ``sorryAx`` (which
is also the completeness signal) and no user-declared axiom.

Two tiers:
  - Unit (offline): the pure predicates (:func:`axioms_clean`, :func:`source_declares_gap`)
    and :func:`soundness_ok` with the sidecar HTTP mocked.
  - Integration (``@pytest.mark.lean``): a real sidecar — a clean proof passes, a
    ``sorry`` proof is rejected via its ``sorryAx`` dependency.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hyperion.crews.soundness import (
    NATIVE_AXIOMS,
    SOUND_BASE,
    allowed_axioms,
    axioms_clean,
    soundness_ok,
    source_declares_gap,
)


def _resp(payload):
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value=payload)
    return m


# ---------------------------------------------------------------------------
# axioms_clean — the subset check that enforces the whole contract
# ---------------------------------------------------------------------------


def test_sound_base_passes_both_modes():
    base = list(SOUND_BASE)
    assert axioms_clean(base, strict=False)
    assert axioms_clean(base, strict=True)


def test_no_axioms_passes():
    assert axioms_clean([], strict=True)


def test_sorryax_rejected_in_both_modes():
    deps = ["propext", "sorryAx"]
    assert not axioms_clean(deps, strict=False)
    assert not axioms_clean(deps, strict=True)


def test_user_axiom_rejected():
    assert not axioms_clean(["propext", "MyProject.myAxiom"], strict=False)


def test_native_axiom_lax_allows_strict_rejects():
    deps = ["Classical.choice", "Lean.ofReduceBool"]
    assert axioms_clean(deps, strict=False), "native_decide tolerated in lax mode"
    assert not axioms_clean(deps, strict=True), "strict (headline) rejects native_decide"


def test_allowed_axioms_sets():
    assert allowed_axioms(strict=True) == SOUND_BASE
    assert allowed_axioms(strict=False) == (SOUND_BASE | NATIVE_AXIOMS)


# ---------------------------------------------------------------------------
# source_declares_gap — cheap textual pre-gate
# ---------------------------------------------------------------------------


def test_source_gap_detects_sorry_and_admit():
    assert source_declares_gap("theorem t : True := by sorry")
    assert source_declares_gap("theorem t : True := by admit")


def test_source_gap_detects_new_axiom():
    assert source_declares_gap("axiom cheat : 1 = 2")


def test_source_gap_ignores_sorry_as_substring():
    # "sorryAx" / "no_sorry_here" as part of a larger identifier is not a gap token.
    assert not source_declares_gap("theorem no_sorry_here : True := trivial")
    assert not source_declares_gap("theorem t : True := trivial -- sorryfree")


def test_source_gap_clean_proof():
    assert not source_declares_gap("theorem t : True := trivial")


# ---------------------------------------------------------------------------
# soundness_ok — sidecar HTTP mocked
# ---------------------------------------------------------------------------


def test_soundness_ok_accepts_clean_proof():
    payload = {"ok": True, "axioms": ["propext", "Classical.choice"], "errors": []}
    with patch("httpx.post", return_value=_resp(payload)):
        res = soundness_ok("theorem t : True := trivial", "t")
    assert res.ok is True
    assert res.infra_ok is True
    assert res.reasons == []


def test_soundness_ok_rejects_sorryax():
    payload = {"ok": True, "axioms": ["sorryAx"], "errors": []}
    with patch("httpx.post", return_value=_resp(payload)):
        # source has no literal sorry (e.g. a hole that became sorryAx via tactic) so the
        # axioms set is the load-bearing signal.
        res = soundness_ok("theorem t : True := by my_tac", "t")
    assert res.ok is False
    assert res.infra_ok is True
    assert any("disallowed" in r for r in res.reasons)


def test_soundness_ok_rejects_literal_sorry_source():
    payload = {"ok": True, "axioms": ["sorryAx"], "errors": []}
    with patch("httpx.post", return_value=_resp(payload)):
        res = soundness_ok("theorem t : True := by sorry", "t")
    assert res.ok is False
    # both the source gate and the axioms check fire
    assert any("sorry" in r for r in res.reasons)


def test_soundness_ok_strict_rejects_native_decide():
    payload = {"ok": True, "axioms": ["Lean.ofReduceBool"], "errors": []}
    with patch("httpx.post", return_value=_resp(payload)):
        lax = soundness_ok("theorem t : x := by native_decide", "t", strict=False)
        strict = soundness_ok("theorem t : x := by native_decide", "t", strict=True)
    assert lax.ok is True
    assert strict.ok is False


def test_soundness_ok_elaboration_failure_not_clean():
    payload = {"ok": False, "axioms": [], "errors": ["unknown identifier 't'"]}
    with patch("httpx.post", return_value=_resp(payload)):
        res = soundness_ok("nonsense", "t")
    assert res.ok is False
    assert res.infra_ok is True
    assert res.reasons


def test_soundness_ok_infra_down_is_retryable_not_false_ok():
    with patch("httpx.post", side_effect=Exception("connection refused")):
        res = soundness_ok("theorem t : True := trivial", "t")
    assert res.infra_ok is False
    assert res.ok is False  # meaningless given infra_ok=False; never a real verdict


# ---------------------------------------------------------------------------
# Integration (live Lean) — real sidecar; skipped unless `lake` is installed
# ---------------------------------------------------------------------------


@pytest.mark.lean
def test_real_clean_proof_passes_contract():
    res = soundness_ok("theorem t : True := trivial", "t", strict=True)
    assert res.infra_ok is True, "Lean sidecar must be reachable for the lean tier"
    assert res.ok is True, res.reasons


@pytest.mark.lean
def test_real_sorry_proof_rejected_by_contract():
    res = soundness_ok("theorem t : True := by sorry", "t")
    assert res.infra_ok is True
    assert res.ok is False
    assert res.reasons
