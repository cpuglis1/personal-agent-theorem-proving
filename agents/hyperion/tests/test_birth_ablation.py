"""Tests for birth ablation (PLAN-definition-synthesis Phase 3).

The same-budget causal test: re-prove the goal WITH the concept's vocabulary in scope
and WITHOUT it at an identical budget. Accept iff solves-WITH (soundness-clean) AND
fails-WITHOUT; ``solves-without`` ⇒ reject (the concept caused nothing).

``prove_proposition`` is patched to script the two arms; the acceptance logic and the
equal-budget invariant are what's under test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hyperion.crews import lean_handlers
from hyperion.crews.lean_handlers import ProofOutcome, birth_ablation_handler
from hyperion.crews.native import NativeNodeCtx
from hyperion.crews.workflows import WorkflowNode
from hyperion.memory.context_store import context_get, context_put


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ctx(task_id: str) -> NativeNodeCtx:
    node = WorkflowNode(id="birth_ablation", kind="native", handler="birth_ablation",
                        instruction="root")
    return NativeNodeCtx(task_id=task_id, node=node, request="prove a = b")


def _concept() -> dict:
    return {
        "concept_id": "c1",
        "definition": {"name": "Balanced", "source": "def Balanced : Prop := True"},
        "bridges": [{"name": "Balanced.x", "source": "theorem Balanced.x : Balanced := pf",
                     "lean_type": "Balanced", "statement": "Balanced.x : Balanced"}],
        "origin": "synthesized",
    }


def _won(clean=True):
    return ProofOutcome(closed=True, source="theorem t := pf", weak_source="theorem t := pf",
                        proof_term="pf", repair_iters=1, axioms=["propext"], axioms_clean=clean)


def _lost():
    return ProofOutcome(closed=False, source=None, weak_source=None, proof_term=None,
                        repair_iters=3, axioms=[], axioms_clean=None)


async def _run(task_id, with_out, without_out, *, concept=True):
    if concept:
        context_put(task_id, "verified_concept:root", _concept())
    pp = AsyncMock(side_effect=[with_out, without_out])
    with patch.object(lean_handlers, "prove_proposition", pp):
        res = await birth_ablation_handler(_ctx(task_id))
    return res, pp


@pytest.mark.anyio
async def test_accept_when_with_solves_and_without_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    res, pp = await _run("ba-acc", _won(), _lost())
    assert res["ok"] is True
    assert res["birth_ablation_pass"] is True
    assert res["with_solves"] is True and res["without_solves"] is False
    accepted = context_get("ba-acc", "accepted_concept:root")
    assert accepted["concept_id"] == "c1"
    assert accepted["provisional"] is True
    assert accepted["necessity_hits"] == 0 and accepted["times_won"] == 1


@pytest.mark.anyio
async def test_reject_crutch_when_without_also_solves(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    res, _ = await _run("ba-crutch", _won(), _won())
    assert res["ok"] is False
    assert res["without_solves"] is True
    rec = context_get("ba-crutch", "birth_ablation:root")
    assert "crutch" in rec["reject_reason"]
    assert context_get("ba-crutch", "accepted_concept:root") is None


@pytest.mark.anyio
async def test_reject_when_with_arm_unsound(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    # WITH arm closes but axioms are dirty ⇒ not soundness-clean ⇒ no causal accept
    res, _ = await _run("ba-unsound", _won(clean=False), _lost())
    assert res["ok"] is False
    assert res["with_solves"] is False


@pytest.mark.anyio
async def test_reject_when_with_arm_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    res, _ = await _run("ba-fail", _lost(), _lost())
    assert res["ok"] is False
    assert res["with_solves"] is False


@pytest.mark.anyio
async def test_equal_budget_across_arms(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    _, pp = await _run("ba-budget", _won(), _lost())
    assert pp.await_count == 2
    with_kwargs = pp.await_args_list[0].kwargs
    without_kwargs = pp.await_args_list[1].kwargs
    # The causal claim collapses unless the budget + regime are identical across arms.
    assert with_kwargs["max_repair"] == without_kwargs["max_repair"]
    assert with_kwargs["weak"] == without_kwargs["weak"]
    # WITH arm has the concept vocabulary in scope; WITHOUT arm does not.
    with_seed = pp.await_args_list[0].args[1]
    without_seed = pp.await_args_list[1].args[1]
    assert "def Balanced" in with_seed
    assert "def Balanced" not in without_seed


@pytest.mark.anyio
async def test_no_concept_does_not_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    pp = AsyncMock()
    with patch.object(lean_handlers, "prove_proposition", pp):
        res = await birth_ablation_handler(_ctx("ba-none"))
    assert res["ok"] is False
    assert res["reason"] == "no verified concept"
    pp.assert_not_called()
