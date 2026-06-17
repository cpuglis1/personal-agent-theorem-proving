"""Phase-4 prover workflow — the orchestration centerpiece (build plan §Phase 4).

Mirrors ``test_subworkflow.py``: the crew stages are mocked (no LLM, no CrewAI), the
Lean kernel is mocked via ``mock_lean`` (targeting the name where the verify controller
uses it), and ``settings.tasks_dir`` is patched to ``tmp_path``. Proves the §Phase 4 DoD:

  - retrieve ‖ synthesize land in ONE wave; verify waits for both (wave grouping).
  - The verify controller DELEGATES each repair proposal to the ``repair`` agent
    (asserted via a patched ``propose_repair``), but ONLY ``verify_lean`` yields a pass —
    a repair that returns a still-broken candidate never produces ``ok`` (the
    oracle-not-faked invariant, §1a).
  - A never-converging repair provably aborts at ``settings.cap_repair_iters``.
  - A full mocked 2-``sorry`` run returns ``status: done`` with a sorry-free
    ``artifacts/result.lean`` and banks the winners.

The live-Lean end-to-end test is written and marked ``@pytest.mark.lean`` (deferred;
skipped where ``lake`` is absent).
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lean_mock import mock_lean

from hyperion.config import settings
from hyperion.crews import lean_handlers, runner
from hyperion.crews.lean_handlers import (
    ProofFailed,
    abstract_handler,
    bank_handler,
    compare_handler,
    verify_handler,
)
from hyperion.crews.native import NativeNodeCtx
from hyperion.crews.workflows import (
    WorkflowNode,
    WorkflowRecord,
    load_workflow,
    topo_sort,
)
from hyperion.memory.context_store import context_get, context_put

_VERIFY_TARGET = ("hyperion.crews.lean_handlers.verify_lean",)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# verify controller — repair delegation + oracle-not-faked + cap (isolated)
# ---------------------------------------------------------------------------


def _verify_ctx(task_id: str, sg: str = "sg") -> NativeNodeCtx:
    node = WorkflowNode(id="verify", kind="native", handler="verify",
                        instruction=sg, upstream=[])
    return NativeNodeCtx(task_id=task_id, node=node, request="theorem target : G", progress_callback=None)


@pytest.mark.anyio
async def test_verify_delegates_repair_but_only_kernel_yields_pass(tmp_path):
    """Path A fails, Path B fails, then a repaired candidate passes — but the pass comes
    from ``verify_lean`` (scripted fail→fail→pass), NOT from the repair proposal, which
    returns a still-broken candidate. Proves repair is delegated yet the kernel alone
    decides ``ok`` (§1a invariant)."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _verify_ctx("v1")
        context_put("v1", "candidate_a:sg", {"source": "a_src", "proof_term": "pa", "statement": "sa"})
        context_put("v1", "candidate_b:sg", {"source": "b_src", "proof_term": "pb", "statement": "sb"})

        repair = AsyncMock(return_value="-- still broken\nexample : G := by sorry")
        with patch.object(lean_handlers, "propose_repair", repair), \
             mock_lean(results=[{"ok": False}, {"ok": False}, {"ok": True}],
                       targets=_VERIFY_TARGET):
            res = await verify_handler(ctx)

        assert res["ok"] is True
        assert res["winner_path"] == "B"
        # Repair WAS delegated (exactly once: one failed B verify, then one repair → pass).
        assert repair.await_count == 1
        decision = context_get("v1", "verify_decision:sg")
        assert decision["repair_iters"] == 1
        assert decision["a_attempts"] == 1


@pytest.mark.anyio
async def test_nonconverging_repair_aborts_at_cap(tmp_path):
    """A repair agent that never converges (kernel always fails) hits
    ``cap_repair_iters`` exactly and fails the sub-goal cleanly (raises ProofFailed),
    instead of spinning. Proves the cap fires."""
    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(settings, "cap_repair_iters", 3):
        ctx = _verify_ctx("v2")
        context_put("v2", "candidate_a:sg", None)
        context_put("v2", "candidate_b:sg", {"source": "b_src", "proof_term": "pb", "statement": "sb"})

        repair = AsyncMock(return_value="example : G := by sorry")
        with patch.object(lean_handlers, "propose_repair", repair), \
             mock_lean(results=[{"ok": False}], targets=_VERIFY_TARGET):
            with pytest.raises(ProofFailed):
                await verify_handler(ctx)

        # One proposal per iteration, capped — no more, no fewer.
        assert repair.await_count == 3
        decision = context_get("v2", "verify_decision:sg")
        assert decision["repair_iters"] == 3
        assert decision["winner_path"] is None


@pytest.mark.anyio
async def test_path_a_wins_without_calling_repair(tmp_path):
    """When a retrieved (Path A) candidate closes the goal, the controller discharges
    via Path A and never touches the repair agent."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _verify_ctx("v3")
        context_put("v3", "candidate_a:sg", {"source": "a_src", "proof_term": "pa", "statement": "sa"})
        repair = AsyncMock()
        with patch.object(lean_handlers, "propose_repair", repair), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            res = await verify_handler(ctx)

        assert res["winner_path"] == "A"
        repair.assert_not_awaited()
        assert context_get("v3", "discharged:sg")["proof_term"] == "pa"


# ---------------------------------------------------------------------------
# Wave grouping — retrieve ‖ synthesize concurrency; verify waits for both
# ---------------------------------------------------------------------------


def test_retrieve_and_synthesize_share_a_wave():
    """In the shipped ``lean-prove`` workflow, retrieve and synthesize share their only
    upstream (skeleton_check), so ``_wave_groups`` places them in the SAME wave (Path A ‖
    Path B), and verify — listing both as upstream — lands in a later wave."""
    wf = load_workflow("lean-prove")
    waves = runner._wave_groups(topo_sort(wf.nodes))
    wave_of = {n.id: wi for wi, wave in enumerate(waves) for n in wave}

    assert wave_of["retrieve"] == wave_of["synthesize"], "retrieve‖synthesize must co-occur in one wave"
    assert wave_of["verify"] > wave_of["retrieve"]
    assert wave_of["verify"] > wave_of["synthesize"]
    # verify genuinely depends on both sourcing nodes.
    verify_node = next(n for n in wf.nodes if n.id == "verify")
    assert set(verify_node.upstream) == {"retrieve", "synthesize"}


# ---------------------------------------------------------------------------
# End-to-end mocked 2-sorry run → sorry-free result.lean
# ---------------------------------------------------------------------------

_SCAFFOLD = (
    "theorem target : P ∧ Q := by\n"
    "  have h1 : P := sorry\n"
    "  have h2 : Q := sorry\n"
    "  exact ⟨h1, h2⟩"
)

_PLAN_MD = f"""---
task_id: prove1
task_type: code
scaffold: |
  {_SCAFFOLD.replace(chr(10), chr(10) + '  ')}
options:
  - id: a
    summary: split the conjunction
    subtasks:
      - id: h1
        description: prove P
        lean_type: "P"
      - id: h2
        description: prove Q
        lean_type: "Q"
---

# decomposition
"""


def _two_sorry_workflow() -> WorkflowRecord:
    """decompose → skeleton_check → (retrieve‖synth → verify → compare → abstract) per
    sub-goal → bank.

    Multi-sorry fan-out as one (retrieve‖synthesize→verify→compare→abstract) chain per
    sub-goal over the shared, sub-goal-namespaced blackboard — each prover native node
    carries its sub-goal id in ``instruction``.
    """
    def chain(sg: str) -> list[WorkflowNode]:
        return [
            WorkflowNode(id=f"retrieve_{sg}", kind="native", handler="retrieve",
                         instruction=sg, upstream=["skeleton_check"]),
            WorkflowNode(id=f"synth_{sg}", kind="work", agent="lemma_synthesizer",
                         instruction=sg, upstream=["skeleton_check"]),
            WorkflowNode(id=f"verify_{sg}", kind="native", handler="verify",
                         instruction=sg, upstream=[f"retrieve_{sg}", f"synth_{sg}"]),
            WorkflowNode(id=f"compare_{sg}", kind="native", handler="compare",
                         instruction=sg, upstream=[f"verify_{sg}"]),
            WorkflowNode(id=f"abstract_{sg}", kind="native", handler="abstract",
                         instruction=sg, upstream=[f"compare_{sg}"]),
        ]

    nodes = [
        WorkflowNode(id="decompose", kind="plan", agent="decomposer", upstream=[]),
        WorkflowNode(id="skeleton_check", kind="native", handler="skeleton_check",
                     upstream=["decompose"]),
        *chain("h1"),
        *chain("h2"),
        WorkflowNode(id="bank", kind="native", handler="bank",
                     upstream=["abstract_h1", "abstract_h2"]),
    ]
    return WorkflowRecord(id="lean-prove-2sorry", name="2-sorry", nodes=nodes)


@contextlib.contextmanager
def _mock_prover_run(tasks_dir):
    """Mock the agent stages, the workflow resolver, retrieval, the bank, and meta tasks
    so the run is LLM-, Qdrant-, and Lean-free. The fake ``_run_stage`` writes plan.md
    (for decompose) and ``candidate_b:<sg>`` (for each synth node)."""

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        base = settings.tasks_dir / task_id
        (base / "notes").mkdir(parents=True, exist_ok=True)
        (base / "artifacts").mkdir(parents=True, exist_ok=True)
        if stage == "decompose":
            (base / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        elif stage.startswith("synth_"):
            sg = stage.split("_", 1)[1]
            context_put(task_id, f"candidate_b:{sg}", {
                "source": f"theorem t_{sg} : G := by trivial",
                "statement": f"theorem t_{sg} : G",
                "proof_term": f"{sg}_synth_proof",
                "origin": "synthesize",
            })

    def _fake_retrieve(goal, **kwargs):
        # Path A applies only to sub-goal h1 (goal type "P"); h2 has no banked lemma.
        if goal.strip() == "P":
            return [{"statement": "lem_P", "proof_term": "lemP_proof", "lean_type": "P"}]
        return []

    registry = {"lean-prove-2sorry": _two_sorry_workflow()}
    store = MagicMock(return_value={"ok": True, "id": "pt", "error": None})

    # The generative lift is delegated; patch it so abstract runs LLM-free (the kernel,
    # mocked ok below, still judges the proposal — propose_abstraction can't fake a pass).
    abstraction = AsyncMock(return_value=[{
        "source": "theorem gen : G := by trivial", "statement": "theorem gen : G",
        "proof_term": "by trivial", "lean_type": "∀ x, G"}])

    with patch.object(settings, "tasks_dir", tasks_dir), \
         patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "load_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_node_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=_fake_stage), \
         patch("hyperion.crews.workflows.resolve_workflow",
               new=MagicMock(side_effect=lambda wid: registry[wid])), \
         patch.object(lean_handlers, "retrieve_applicable_lemmas", _fake_retrieve), \
         patch.object(lean_handlers, "propose_abstraction", abstraction), \
         patch("hyperion.memory.lemma_bank.store_lemma", store), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()), \
         mock_lean(ok=True, targets=_VERIFY_TARGET):
        yield store


@pytest.mark.anyio
async def test_end_to_end_two_sorry_run_produces_sorry_free_result(tmp_path):
    """A full mocked run over a 2-sorry scaffold returns done with a sorry-free
    result.lean: sub-goal h1 discharged via Path A (retrieval), h2 via Path B
    (synthesis), both stitched into the scaffold and banked."""
    with _mock_prover_run(tmp_path) as store:
        result = await runner.run_task("prove1", "prove P ∧ Q", workflow="lean-prove-2sorry")

    assert result["status"] == "done", result
    result_lean = tmp_path / "prove1" / "artifacts" / "result.lean"
    assert result_lean.exists()
    text = result_lean.read_text(encoding="utf-8")
    assert "sorry" not in text
    assert "lemP_proof" in text        # h1 closed via Path A
    assert "h2_synth_proof" in text    # h2 closed via Path B
    # Both winners were banked (loud store path invoked per discharged sub-goal).
    assert store.call_count == 2


@pytest.mark.anyio
async def test_bank_surfaces_a_failed_write(tmp_path):
    """A failed ``store_lemma`` (ok=False) is surfaced in the bank node's result
    (``bank_failures``), never silently swallowed (risk #4 / load-bearing write path).
    Exercised on the handler directly so the surfaced result dict is observable."""
    from hyperion.crews.lean_handlers import bank_handler

    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "prove2").mkdir(parents=True, exist_ok=True)
        (tmp_path / "prove2" / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        context_put("prove2", "discharged:h1",
                    {"proof_term": "lemP_proof", "statement": "lem_P", "path": "A", "lean_type": "P"})
        context_put("prove2", "discharged:h2",
                    {"proof_term": "h2_synth_proof", "statement": "t_h2", "path": "B", "lean_type": "Q"})
        node = WorkflowNode(id="bank", kind="native", handler="bank", upstream=[])
        ctx = NativeNodeCtx(task_id="prove2", node=node, request="prove P ∧ Q", progress_callback=None)

        store = MagicMock(return_value={"ok": False, "id": "pt", "error": "qdrant down"})
        with patch("hyperion.memory.lemma_bank.store_lemma", store), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            res = await bank_handler(ctx)

        # The loss is surfaced, not swallowed; the artifact is still assembled.
        assert len(res["bank_failures"]) == 2
        assert res["n_banked"] == 0
        assert all(f["error"] == "qdrant down" for f in res["bank_failures"])
        assert (tmp_path / "prove2" / "artifacts" / "result.lean").exists()


# ---------------------------------------------------------------------------
# Phase 5 — compare/abstract wiring (after verify, before bank) + anti-starvation
# ---------------------------------------------------------------------------


def _native_ctx(task_id: str, node_id: str, handler: str, sg: str = "h1",
                request: str = "prove P ∧ Q") -> NativeNodeCtx:
    node = WorkflowNode(id=node_id, kind="native", handler=handler,
                        instruction=sg, upstream=[])
    return NativeNodeCtx(task_id=task_id, node=node, request=request, progress_callback=None)


def test_compare_and_abstract_run_between_verify_and_bank():
    """In the shipped ``lean-prove`` workflow, ``compare`` runs after ``verify``,
    ``abstract`` after ``compare``, and ``bank`` after ``abstract`` (bank receives the
    abstracted form)."""
    wf = load_workflow("lean-prove")
    waves = runner._wave_groups(topo_sort(wf.nodes))
    wave_of = {n.id: wi for wi, wave in enumerate(waves) for n in wave}

    assert wave_of["compare"] > wave_of["verify"]
    assert wave_of["abstract"] > wave_of["compare"]
    assert wave_of["bank"] > wave_of["abstract"]
    bank = next(n for n in wf.nodes if n.id == "bank")
    assert bank.upstream == ["abstract"]


@pytest.mark.anyio
async def test_research_mode_abstracts_path_b_even_when_path_a_wins_and_bank_stores_it(tmp_path):
    """RESEARCH mode end-to-end over verify→compare→abstract→bank for one sub-goal:
      - verify kernel-verifies BOTH paths (verified_a AND verified_b set);
      - compare logs a genuine A-vs-B contest (``compared``) and Path A wins;
      - abstract STILL fires on the fresh Path-B lemma (anti-starvation, decision e);
      - bank stores the ABSTRACTED (generalized) form, not the concrete winner.
    """
    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(settings, "prover_research_mode", True):
        (tmp_path / "r1").mkdir(parents=True, exist_ok=True)
        (tmp_path / "r1" / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        context_put("r1", "candidate_a:h1",
                    {"source": "a_src", "statement": "lem_P", "proof_term": "pa", "lean_type": "P"})
        context_put("r1", "candidate_b:h1",
                    {"source": "b_src", "statement": "synth_P", "proof_term": "pb", "lean_type": "P"})

        proposal = [{"source": "theorem g {α} (x : α) : x = x := rfl",
                     "statement": "theorem g {α} (x : α) : x = x",
                     "proof_term": "rfl", "lean_type": "∀ {α} (x : α), x = x"}]
        store = MagicMock(return_value={"ok": True, "id": "pt", "error": None})

        with patch.object(lean_handlers, "propose_abstraction", AsyncMock(return_value=proposal)), \
             patch("hyperion.memory.lemma_bank.store_lemma", store), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            await verify_handler(_native_ctx("r1", "verify", "verify"))
            # RESEARCH: both paths were kernel-verified.
            assert context_get("r1", "verified_a:h1") is not None
            assert context_get("r1", "verified_b:h1") is not None

            compare_res = await compare_handler(_native_ctx("r1", "compare", "compare"))
            assert compare_res["compared"] is True       # genuine A-vs-B contest
            assert compare_res["winner_path"] == "A"      # reuse-first tie → Path A wins

            abstract_res = await abstract_handler(_native_ctx("r1", "abstract", "abstract"))
            assert abstract_res["fired"] is True          # fired despite Path A winning
            assert abstract_res["abstracted"] is True

            await bank_handler(_native_ctx("r1", "bank", "bank"))

        # The bank received the generalized lemma (the abstracted form), not "lem_P".
        assert store.call_count == 1
        banked_statement = store.call_args_list[0].args[0]
        assert banked_statement == "theorem g {α} (x : α) : x = x"


# ---------------------------------------------------------------------------
# Integration (live Lean) — written, deferred (no `lake` here)
# ---------------------------------------------------------------------------


@pytest.mark.lean
@pytest.mark.anyio
async def test_real_trivial_theorem_end_to_end(tmp_path):
    """A real trivial theorem proved end-to-end through the workflow against the live
    sidecar. Deferred: requires the Mathlib sidecar image + `lake` (conftest skips when
    `lake` is absent). Exercises the unmocked verify_lean as the verdict."""
    plan_md = """---
task_id: live1
task_type: code
scaffold: |
  theorem t : True := by
    have h1 : True := sorry
    exact h1
options:
  - id: a
    summary: trivial
    subtasks:
      - id: h1
        description: prove True
        lean_type: "True"
---
"""

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        base = settings.tasks_dir / task_id
        (base / "notes").mkdir(parents=True, exist_ok=True)
        (base / "artifacts").mkdir(parents=True, exist_ok=True)
        if stage == "decompose":
            (base / "plan.md").write_text(plan_md, encoding="utf-8")
        elif stage.startswith("synth_"):
            sg = stage.split("_", 1)[1]
            context_put(task_id, f"candidate_b:{sg}", {
                "source": "theorem t_h1 : True := trivial",
                "statement": "theorem t_h1 : True",
                "proof_term": "trivial",
                "origin": "synthesize",
            })

    def triple(sg):
        return [
            WorkflowNode(id=f"retrieve_{sg}", kind="native", handler="retrieve",
                         instruction=sg, upstream=["skeleton_check"]),
            WorkflowNode(id=f"synth_{sg}", kind="work", agent="lemma_synthesizer",
                         instruction=sg, upstream=["skeleton_check"]),
            WorkflowNode(id=f"verify_{sg}", kind="native", handler="verify",
                         instruction=sg, upstream=[f"retrieve_{sg}", f"synth_{sg}"]),
        ]

    wf = WorkflowRecord(id="lean-prove-live", name="live", nodes=[
        WorkflowNode(id="decompose", kind="plan", agent="decomposer", upstream=[]),
        WorkflowNode(id="skeleton_check", kind="native", handler="skeleton_check",
                     upstream=["decompose"]),
        *triple("h1"),
        WorkflowNode(id="bank", kind="native", handler="bank", upstream=["verify_h1"]),
    ])

    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "load_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_node_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=_fake_stage), \
         patch("hyperion.crews.workflows.resolve_workflow", new=MagicMock(return_value=wf)), \
         patch.object(lean_handlers, "retrieve_applicable_lemmas", lambda goal, **k: []), \
         patch("hyperion.memory.lemma_bank.store_lemma",
               MagicMock(return_value={"ok": True, "id": "x", "error": None})), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()):
        result = await runner.run_task("live1", "prove True", workflow="lean-prove-live")

    assert result["status"] == "done"
    text = (tmp_path / "live1" / "artifacts" / "result.lean").read_text(encoding="utf-8")
    assert "sorry" not in text
