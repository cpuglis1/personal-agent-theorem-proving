"""The anti-unification abstractor — native controller + the §1a-style generative seam.

Build plan §Phase 5 Test Gate:
  - Abstractor lifts a constant → re-verifies (``mock_lean`` for unit).
  - Most-general form that type-checks is kept; over-abstraction is rejected and falls
    back to the concrete verified lemma.
  - Anti-starvation: abstraction fires on a fresh Path-B lemma even when Path A also
    closed the goal (the controller reads ``verified_b`` independent of the winner).
  - (Integration, ``@pytest.mark.lean``) a real lemma abstracted, re-verified against
    live Lean, over-abstraction rejected — written, deferred where ``lake`` is absent.

The controller owns the deterministic re-verify + fallback; only the lift is delegated to
``propose_abstraction``, which is patched here (mirroring how ``propose_repair`` is patched
in the verify tests) so the unit tier is LLM-free and the kernel-judges invariant holds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from lean_mock import mock_lean

from hyperion.config import settings
from hyperion.crews import lean_handlers
from hyperion.crews.lean_handlers import abstract_handler
from hyperion.crews.native import NativeNodeCtx
from hyperion.crews.workflows import WorkflowNode
from hyperion.memory.context_store import context_get, context_put

_VERIFY_TARGET = ("hyperion.crews.lean_handlers.verify_lean",)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _abstract_ctx(task_id: str, sg: str = "sg") -> NativeNodeCtx:
    node = WorkflowNode(id="abstract", kind="native", handler="abstract",
                        instruction=sg, upstream=[])
    return NativeNodeCtx(task_id=task_id, node=node, request="theorem target : G",
                         progress_callback=None)


# ---------------------------------------------------------------------------
# Anti-starvation trigger
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_op_when_no_fresh_path_b_lemma(tmp_path):
    """With ``verified_b`` unset (Path B never verified — DEPLOY mode after Path A won),
    abstract cleanly no-ops: nothing fresh to generalize."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _abstract_ctx("a0")
        context_put("a0", "verified_b:sg", None)
        repair = AsyncMock()  # propose_abstraction must not even be called
        with patch.object(lean_handlers, "propose_abstraction", repair), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            res = await abstract_handler(ctx)
        assert res["fired"] is False
        repair.assert_not_awaited()
        assert context_get("a0", "abstracted:sg") is None


@pytest.mark.anyio
async def test_fires_on_fresh_path_b_even_when_path_a_won(tmp_path):
    """Anti-starvation: Path A won the compare (``discharged`` is Path A), yet Path B also
    produced a verified lemma (``verified_b`` set) — abstract STILL fires on the Path-B
    lemma and the generalized form lands in ``abstracted:<sg>`` for the bank."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _abstract_ctx("a1")
        context_put("a1", "discharged:sg", {"proof_term": "pa", "path": "A", "lean_type": "P"})
        context_put("a1", "verified_b:sg",
                    {"source": "theorem t : P := by trivial", "statement": "theorem t : P",
                     "proof_term": "by trivial", "lean_type": "P", "path": "B"})
        proposal = [{"source": "theorem t {x : Prop} : x → x := fun h => h",
                     "statement": "theorem t {x : Prop} : x → x",
                     "proof_term": "fun h => h", "lean_type": "∀ {x : Prop}, x → x"}]
        with patch.object(lean_handlers, "propose_abstraction",
                          AsyncMock(return_value=proposal)), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            res = await abstract_handler(ctx)
        assert res["fired"] is True
        assert res["abstracted"] is True
        abstracted = context_get("a1", "abstracted:sg")
        assert abstracted["origin"] == "abstract"
        assert abstracted["lean_type"] == "∀ {x : Prop}, x → x"


# ---------------------------------------------------------------------------
# Lift → re-verify; most-general-that-type-checks; over-abstraction rejected
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_lifts_constant_and_reverifies(tmp_path):
    """A liftable constant → a more-general statement that re-verifies (kernel ok=True).
    The chosen, generalized lemma is written to ``abstracted:<sg>``."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _abstract_ctx("a2")
        context_put("a2", "verified_b:sg",
                    {"source": "example : 0 + 0 = 0 := rfl", "statement": "0 + 0 = 0",
                     "proof_term": "rfl", "lean_type": "0 + 0 = 0", "path": "B"})
        proposal = [{"source": "theorem add_zero (n : Nat) : n + 0 = n := by simp",
                     "statement": "theorem add_zero (n : Nat) : n + 0 = n",
                     "proof_term": "by simp", "lean_type": "(n : Nat) → n + 0 = n"}]
        with patch.object(lean_handlers, "propose_abstraction",
                          AsyncMock(return_value=proposal)), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            res = await abstract_handler(ctx)
        assert res["abstracted"] is True
        assert res["n_rejected"] == 0
        abstracted = context_get("a2", "abstracted:sg")
        assert abstracted["proof_term"] == "by simp"
        assert abstracted["generality_score"] >= 1.0  # gained a binder


@pytest.mark.anyio
async def test_keeps_most_general_that_type_checks(tmp_path):
    """Proposals are most-general-first; the kernel rejects the boldest (fail) and accepts
    the next (pass). The controller keeps that second form and records one rejection."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _abstract_ctx("a3")
        context_put("a3", "verified_b:sg",
                    {"source": "example : P := proof", "statement": "P",
                     "proof_term": "proof", "lean_type": "P", "path": "B"})
        proposals = [
            {"source": "TOO_GENERAL", "statement": "tg", "proof_term": "x", "lean_type": "∀ a b, a = b"},
            {"source": "JUST_RIGHT", "statement": "jr", "proof_term": "y", "lean_type": "∀ a, a = a"},
        ]
        with patch.object(lean_handlers, "propose_abstraction",
                          AsyncMock(return_value=proposals)), \
             mock_lean(results=[{"ok": False}, {"ok": True}], targets=_VERIFY_TARGET):
            res = await abstract_handler(ctx)
        assert res["abstracted"] is True
        assert res["n_rejected"] == 1
        assert context_get("a3", "abstracted:sg")["source"] == "JUST_RIGHT"


@pytest.mark.anyio
async def test_over_abstraction_rejected_falls_back_to_concrete(tmp_path):
    """Every proposal is an over-abstraction the kernel rejects → fall back to the concrete
    verified Path-B lemma (origin ``abstract-fallback``), never banking a non-type-checking
    generalization."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _abstract_ctx("a4")
        concrete = {"source": "example : P := proof", "statement": "P",
                    "proof_term": "proof", "lean_type": "P", "path": "B"}
        context_put("a4", "verified_b:sg", concrete)
        proposals = [{"source": "OVERGENERAL", "statement": "og", "proof_term": "z",
                      "lean_type": "∀ a b, a = b"}]
        with patch.object(lean_handlers, "propose_abstraction",
                          AsyncMock(return_value=proposals)), \
             mock_lean(ok=False, targets=_VERIFY_TARGET):
            res = await abstract_handler(ctx)
        assert res["abstracted"] is False
        assert res["n_rejected"] == 1
        fell_back = context_get("a4", "abstracted:sg")
        assert fell_back["origin"] == "abstract-fallback"
        assert fell_back["proof_term"] == "proof"   # the concrete proof, unchanged


@pytest.mark.anyio
async def test_no_proposals_falls_back_to_concrete(tmp_path):
    """When the abstractor offers nothing (e.g. a flaky proxy → ``[]``), fall back to the
    concrete verified lemma rather than dropping it."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _abstract_ctx("a5")
        context_put("a5", "verified_b:sg",
                    {"statement": "P", "proof_term": "proof", "lean_type": "P", "path": "B"})
        with patch.object(lean_handlers, "propose_abstraction", AsyncMock(return_value=[])), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            res = await abstract_handler(ctx)
        assert res["abstracted"] is False
        assert context_get("a5", "abstracted:sg")["origin"] == "abstract-fallback"


# ---------------------------------------------------------------------------
# Integration (live Lean) — written, deferred (no `lake` here)
# ---------------------------------------------------------------------------


@pytest.mark.lean
@pytest.mark.anyio
async def test_real_lemma_abstracted_overabstraction_rejected(tmp_path):
    """A REAL lemma abstracted and re-verified against live Lean: the controller is given
    a most-general-first ladder whose boldest rung does NOT type-check and whose next rung
    does. The live kernel must reject the over-abstraction and keep the valid generalization
    (most-general-that-type-checks). Deferred: needs the Mathlib sidecar + `lake` (conftest
    skips when `lake` is absent). ``propose_abstraction`` is patched (this tier tests the
    Lean re-verification + fallback, not the LLM); ``verify_lean`` runs for real."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _abstract_ctx("alive")
        context_put("alive", "verified_b:sg",
                    {"source": "theorem z : 0 + 0 = 0 := by rfl", "statement": "0 + 0 = 0",
                     "proof_term": "by rfl", "lean_type": "0 + 0 = 0", "path": "B"})
        proposals = [
            # Boldest: a false over-generalization the kernel must reject.
            {"source": "theorem over (a b : Nat) : a + b = 0 := by rfl",
             "statement": "theorem over (a b : Nat) : a + b = 0",
             "proof_term": "by rfl", "lean_type": "(a b : Nat) → a + b = 0"},
            # Valid generalization that really type-checks.
            {"source": "theorem ok (n : Nat) : n + 0 = n := by simp",
             "statement": "theorem ok (n : Nat) : n + 0 = n",
             "proof_term": "by simp", "lean_type": "(n : Nat) → n + 0 = n"},
        ]
        with patch.object(lean_handlers, "propose_abstraction",
                          AsyncMock(return_value=proposals)):
            res = await abstract_handler(ctx)

    assert res["abstracted"] is True
    assert res["n_rejected"] == 1            # the live kernel rejected the over-abstraction
    chosen = context_get("alive", "abstracted:sg")
    assert "n + 0 = n" in chosen["statement"]
