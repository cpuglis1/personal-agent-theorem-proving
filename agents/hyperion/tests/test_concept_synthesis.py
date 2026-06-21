"""Tests for definition synthesis (PLAN-definition-synthesis Phase 2).

Covers the three deliverables that don't need an LLM or a live Lean:
  - ``definition_degeneracy_reasons`` — the pure, cheap pre-proving gates.
  - ``synthesize_definition_handler`` — propose (mocked) → gate → stage survivors.
  - ``verify_concept_handler`` — elaborate def (mocked Lean) + prove every bridge
    soundness-clean (mocked ``prove_proposition``), keeping the first full pass.

The proposer/prover/kernel seams are patched; the control flow + acceptance logic is
what's under test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hyperion.crews import lean_handlers
from hyperion.crews.lean_handlers import (
    ProofOutcome,
    definition_degeneracy_reasons,
    synthesize_definition_handler,
    verify_concept_handler,
)
from hyperion.crews.native import NativeNodeCtx
from hyperion.crews.workflows import WorkflowNode
from hyperion.memory.context_store import context_get, context_put
from lean_mock import mock_lean

_VERIFY_TARGET = ("hyperion.crews.lean_handlers.verify_lean",)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ctx(task_id: str, handler: str) -> NativeNodeCtx:
    # instruction names the sub-goal id this node operates on ⇒ blackboard keys are
    # namespaced "<base>:root" (see _subgoal_id / _bb_key).
    node = WorkflowNode(id=handler, kind="native", handler=handler, instruction="root")
    return NativeNodeCtx(task_id=task_id, node=node, request="prove something")


def _good_candidate() -> dict:
    return {
        "definition": {
            "name": "Balanced",
            "source": "def Balanced (xs : List Nat) : Prop := xs.length % 2 = 0",
        },
        "bridges": [
            {
                "name": "Balanced.nil",
                "source": "theorem Balanced.nil : Balanced [] := by decide",
                "lean_type": "Balanced []",
                "statement": "Balanced.nil : Balanced []",
            }
        ],
    }


# ---------------------------------------------------------------------------
# definition_degeneracy_reasons — pure gates
# ---------------------------------------------------------------------------


def test_good_candidate_passes_gates():
    assert definition_degeneracy_reasons(_good_candidate(), parent_name="myThm") == []


def test_rejects_trivial_true_false_body():
    cand = {"definition": {"name": "X", "source": "def X : Prop := True"},
            "bridges": [{"name": "x", "source": "theorem x : X := trivial"}]}
    reasons = definition_degeneracy_reasons(cand)
    assert any("True/False" in r for r in reasons)


def test_rejects_sorry_in_definition():
    cand = _good_candidate()
    cand["definition"]["source"] = "def Balanced (xs : List Nat) : Prop := by sorry"
    reasons = definition_degeneracy_reasons(cand)
    assert any("sorry" in r for r in reasons)


def test_rejects_parent_name_mention():
    cand = _good_candidate()
    cand["definition"]["source"] = "def Balanced := myParentThm"
    reasons = definition_degeneracy_reasons(cand, parent_name="myParentThm")
    assert any("parent theorem name" in r for r in reasons)


def test_rejects_defeq_to_parent_goal():
    cand = _good_candidate()
    cand["definition"]["source"] = "def G : Prop := a = b"
    reasons = definition_degeneracy_reasons(cand, parent_goal="a = b")
    assert any("defeq to the parent goal" in r for r in reasons)


def test_rejects_no_bridges():
    cand = {"definition": {"name": "B", "source": "def B : Prop := 1 = 1"}, "bridges": []}
    reasons = definition_degeneracy_reasons(cand)
    assert any("no bridge lemmas" in r for r in reasons)


def test_rejects_bridge_with_sorry():
    cand = _good_candidate()
    cand["bridges"][0]["source"] = "theorem Balanced.nil : Balanced [] := by sorry"
    reasons = definition_degeneracy_reasons(cand)
    assert any("bridge 0" in r and "sorry" in r for r in reasons)


# ---------------------------------------------------------------------------
# synthesize_definition_handler — propose → gate → stage
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_synthesize_stages_only_survivors(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    good = _good_candidate()
    bad = {"definition": {"name": "X", "source": "def X : Prop := True"}, "bridges": []}
    propose = AsyncMock(return_value=[good, bad])
    ctx = _ctx("t-syn", "synthesize_definition")
    with patch.object(lean_handlers, "propose_definition", propose):
        res = await synthesize_definition_handler(ctx)
    assert res["ok"] is True
    assert res["n_proposed"] == 2
    assert res["n_survived"] == 1
    staged = context_get("t-syn", "concept_candidates:root")
    assert len(staged) == 1
    assert staged[0]["definition"]["name"] == "Balanced"
    assert staged[0]["concept_id"]  # id stamped


@pytest.mark.anyio
async def test_synthesize_no_candidates_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    propose = AsyncMock(return_value=[])
    ctx = _ctx("t-syn0", "synthesize_definition")
    with patch.object(lean_handlers, "propose_definition", propose):
        res = await synthesize_definition_handler(ctx)
    assert res["ok"] is False
    assert context_get("t-syn0", "concept_candidates:root") == []


# ---------------------------------------------------------------------------
# verify_concept_handler — elaborate def + prove bridges soundness-clean
# ---------------------------------------------------------------------------


def _won(axioms_clean=True):
    return ProofOutcome(
        closed=True, source="theorem t := pf", weak_source="theorem t := pf",
        proof_term="pf", repair_iters=0, axioms=["propext"], axioms_clean=axioms_clean,
    )


def _lost():
    return ProofOutcome(closed=False, source=None, weak_source=None, proof_term=None,
                        repair_iters=3, axioms=[], axioms_clean=None)


@pytest.mark.anyio
async def test_verify_concept_accepts_clean_package(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    cand = {**_good_candidate(), "concept_id": "c1"}
    context_put("t-vc", "concept_candidates:root", [cand])
    pp = AsyncMock(return_value=_won())
    ctx = _ctx("t-vc", "verify_concept")
    with patch.object(lean_handlers, "prove_proposition", pp), mock_lean(
        ok=True, targets=_VERIFY_TARGET
    ):
        res = await verify_concept_handler(ctx)
    assert res["ok"] is True
    assert res["concept_id"] == "c1"
    verified = context_get("t-vc", "verified_concept:root")
    assert verified["definition"]["name"] == "Balanced"
    assert verified["bridges"][0]["name"] == "Balanced.nil"


@pytest.mark.anyio
async def test_verify_concept_rejects_when_bridge_unsound(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    cand = {**_good_candidate(), "concept_id": "c1"}
    context_put("t-vc2", "concept_candidates:root", [cand])
    # bridge closes but axioms are dirty (e.g. sorryAx) ⇒ not soundness-clean ⇒ reject
    pp = AsyncMock(return_value=_won(axioms_clean=False))
    ctx = _ctx("t-vc2", "verify_concept")
    with patch.object(lean_handlers, "prove_proposition", pp), mock_lean(
        ok=True, targets=_VERIFY_TARGET
    ):
        res = await verify_concept_handler(ctx)
    assert res["ok"] is False
    assert context_get("t-vc2", "verified_concept:root") is None


@pytest.mark.anyio
async def test_verify_concept_rejects_when_definition_fails_to_elaborate(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    cand = {**_good_candidate(), "concept_id": "c1"}
    context_put("t-vc3", "concept_candidates:root", [cand])
    pp = AsyncMock(return_value=_won())
    ctx = _ctx("t-vc3", "verify_concept")
    # definition does not elaborate ⇒ candidate skipped; prove_proposition never called
    with patch.object(lean_handlers, "prove_proposition", pp), mock_lean(
        ok=False, targets=_VERIFY_TARGET
    ):
        res = await verify_concept_handler(ctx)
    assert res["ok"] is False
    pp.assert_not_called()


@pytest.mark.anyio
async def test_verify_concept_rejects_vacuous_definition(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    cand = {**_good_candidate(), "concept_id": "c1",
            "vacuity_probe": "example : Balanced [] := by trivial"}
    context_put("t-vc4", "concept_candidates:root", [cand])
    pp = AsyncMock(return_value=_won())
    ctx = _ctx("t-vc4", "verify_concept")
    # Both the def elaboration AND the vacuity probe verify ok=True; the probe passing
    # means the concept is vacuous ⇒ reject before proving bridges.
    with patch.object(lean_handlers, "prove_proposition", pp), mock_lean(
        ok=True, targets=_VERIFY_TARGET
    ):
        res = await verify_concept_handler(ctx)
    assert res["ok"] is False
    pp.assert_not_called()


@pytest.mark.anyio
async def test_verify_concept_keeps_first_passing_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERION_TASKS_DIR", str(tmp_path))
    c1 = {**_good_candidate(), "concept_id": "c1"}
    c2 = {**_good_candidate(), "concept_id": "c2"}
    context_put("t-vc5", "concept_candidates:root", [c1, c2])
    # first candidate's bridge fails to close, second's succeeds
    pp = AsyncMock(side_effect=[_lost(), _won()])
    ctx = _ctx("t-vc5", "verify_concept")
    with patch.object(lean_handlers, "prove_proposition", pp), mock_lean(
        ok=True, targets=_VERIFY_TARGET
    ):
        res = await verify_concept_handler(ctx)
    assert res["ok"] is True
    assert res["concept_id"] == "c2"
