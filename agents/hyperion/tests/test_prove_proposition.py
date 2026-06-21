"""Tests for ``prove_proposition`` — the reusable proving kernel (Phase 1).

The kernel extracted from ``verify_handler`` Path B: verify a seed source, run the
bounded repair loop on failure, honor the weak-prover gate, and (when a ``decl`` is
given) apply the soundness contract. Bridges, planned lemmas, and the same-budget
ablation re-proofs (Phases 2-4) all call it, so its contract is tested directly here.

Lean is mocked via ``mock_lean`` (targeting the name where the kernel imports it);
``propose_repair`` and ``soundness_ok`` are patched as the module seams.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hyperion.crews import lean_handlers
from hyperion.crews.lean_handlers import prove_proposition
from hyperion.crews.soundness import SoundnessResult
from lean_mock import mock_lean

_TARGET = ("hyperion.crews.lean_handlers.verify_lean",)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_seed_closes_no_repair():
    with mock_lean(ok=True, targets=_TARGET):
        out = await prove_proposition("p", "example : p := by exact hp")
    assert out.closed is True
    assert out.won is True
    assert out.source == "example : p := by exact hp"
    assert out.weak_source == out.source
    assert out.repair_iters == 0
    assert [v["path"] for v in out.verdicts] == ["seed"]
    assert out.axioms_clean is None  # no decl ⇒ soundness probe skipped


@pytest.mark.anyio
async def test_repair_closes_on_second_round():
    repair = AsyncMock(return_value="example : p := by exact hp")
    # seed fails, first repair fails, second repair closes.
    with patch.object(lean_handlers, "propose_repair", repair), mock_lean(
        results=[{"ok": False}, {"ok": False}, {"ok": True}], targets=_TARGET
    ):
        out = await prove_proposition("p", "example : p := by sorry_placeholder")
    assert out.closed is True
    assert out.won is True
    assert out.repair_iters == 2
    assert repair.await_count == 2
    assert [v["path"] for v in out.verdicts] == ["seed", "repair", "repair"]


@pytest.mark.anyio
async def test_never_closes_exhausts_repair_budget():
    repair = AsyncMock(return_value="example : p := by still_broken")
    with patch.object(lean_handlers, "propose_repair", repair), mock_lean(
        ok=False, targets=_TARGET
    ):
        out = await prove_proposition("p", "example : p := by broken", max_repair=2)
    assert out.closed is False
    assert out.won is False
    assert out.source is None and out.weak_source is None
    assert out.repair_iters == 2
    assert repair.await_count == 2


@pytest.mark.anyio
async def test_weak_gate_keeps_strong_as_counterfactual_then_repairs_to_weak():
    # Seed closes but uses a banned strong closer (omega) ⇒ strong counterfactual only.
    # The repair returns a weak-tactic proof ⇒ that becomes the win-eligible proof.
    repair = AsyncMock(return_value="example : p := by exact hp")
    with patch.object(lean_handlers, "propose_repair", repair), mock_lean(
        ok=True, targets=_TARGET
    ):
        out = await prove_proposition("p", "example : p := by omega", weak=True, max_repair=2)
    assert out.closed is True  # strong close exists (the counterfactual)
    assert out.source == "example : p := by omega"
    assert out.won is True
    assert out.weak_source == "example : p := by exact hp"
    assert out.repair_iters == 1


@pytest.mark.anyio
async def test_weak_gate_strong_only_when_repair_never_weak():
    # Every attempt closes but always with a banned closer ⇒ strong counterfactual, no win.
    repair = AsyncMock(return_value="example : p := by omega")
    with patch.object(lean_handlers, "propose_repair", repair), mock_lean(
        ok=True, targets=_TARGET
    ):
        out = await prove_proposition("p", "example : p := by ring", weak=True, max_repair=2)
    assert out.closed is True
    assert out.source == "example : p := by ring"
    assert out.won is False
    assert out.weak_source is None
    assert out.repair_iters == 2  # kept trying for an eligible proof, never found one


@pytest.mark.anyio
async def test_decl_runs_soundness_contract():
    clean = SoundnessResult(ok=True, infra_ok=True, axioms=["propext"])
    with patch.object(lean_handlers, "soundness_ok", return_value=clean) as sound, mock_lean(
        ok=True, targets=_TARGET
    ):
        out = await prove_proposition("p", "theorem t : p := by exact hp", decl="t")
    assert out.axioms_clean is True
    assert out.axioms == ["propext"]
    sound.assert_called_once()
    # the soundness contract runs on the winning source, with the supplied decl
    assert sound.call_args.args[0] == "theorem t : p := by exact hp"
    assert sound.call_args.args[1] == "t"


@pytest.mark.anyio
async def test_decl_soundness_rejects_dirty_axioms():
    dirty = SoundnessResult(ok=False, infra_ok=True, axioms=["sorryAx"], reasons=["disallowed axioms: sorryAx"])
    with patch.object(lean_handlers, "soundness_ok", return_value=dirty), mock_lean(
        ok=True, targets=_TARGET
    ):
        out = await prove_proposition("p", "theorem t : p := by tac", decl="t")
    # The kernel said closed, but the soundness contract rejects it: closed yet not clean.
    assert out.closed is True
    assert out.axioms_clean is False


@pytest.mark.anyio
async def test_no_win_skips_soundness_probe():
    with patch.object(lean_handlers, "soundness_ok") as sound, mock_lean(
        ok=False, targets=_TARGET
    ):
        out = await prove_proposition("p", "theorem t : p := by broken", decl="t", max_repair=0)
    assert out.closed is False
    assert out.axioms_clean is None
    sound.assert_not_called()
