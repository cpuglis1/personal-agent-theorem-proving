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
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lean_mock import mock_lean

from hyperion.config import settings
from hyperion.crews import lean_handlers, runner
from hyperion.crews.lean_handlers import (
    ProofFailed,
    ProofOutcome,
    formal_ingest_handler,
    lean_decompose_handler,
    abstract_handler,
    bank_concept_handler,
    bank_handler,
    compare_handler,
    escalation_gate_handler,
    skeleton_check_handler,
    verify_handler,
)
from hyperion.crews.native import NativeNodeCtx
from hyperion.crews.plan_contract import parse_plan
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
# Native Lean decomposition — deterministic plan writer for live smoke path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_native_lean_decomposer_writes_parseable_single_subgoal_plan(tmp_path):
    """The native decomposer writes the exact plan contract consumed downstream."""
    with patch.object(settings, "tasks_dir", tmp_path):
        node = WorkflowNode(id="decompose", kind="native", handler="lean_decompose", upstream=[])
        ctx = NativeNodeCtx(
            task_id="native_decomp",
            node=node,
            request="Prove that 0 + 0 = 0.",
            progress_callback=None,
        )

        res = await lean_decompose_handler(ctx)
        plan = parse_plan("native_decomp")

    assert res["goal"] == "0 + 0 = 0"
    assert plan.selected_option == "a"
    assert plan.scaffold == "have h1 : 0 + 0 = 0 := sorry\nexact h1\n"
    subs = plan.active_subtasks()
    assert len(subs) == 1
    assert subs[0].id == "h1"
    assert subs[0].description == "Prove the target proposition."
    assert subs[0].lean_type == "0 + 0 = 0"


@pytest.mark.anyio
async def test_native_decomposer_scaffold_wraps_for_skeleton_check(tmp_path):
    """The body-only native scaffold is verified via the existing command wrapper."""
    with patch.object(settings, "tasks_dir", tmp_path):
        decompose = WorkflowNode(id="decompose", kind="native", handler="lean_decompose", upstream=[])
        await lean_decompose_handler(NativeNodeCtx(
            task_id="native_skeleton",
            node=decompose,
            request="Prove that True.",
            progress_callback=None,
        ))
        check = WorkflowNode(
            id="skeleton_check", kind="native", handler="skeleton_check", upstream=["decompose"]
        )
        ctx = NativeNodeCtx(
            task_id="native_skeleton",
            node=check,
            request="Prove that True.",
            progress_callback=None,
        )

        with mock_lean(ok=True, targets=_VERIFY_TARGET) as lean:
            res = await skeleton_check_handler(ctx)

    assert res["ok"] is True
    source = lean.call_args.args[0]
    assert source == "example : True := by\n  have h1 : True := sorry\n  exact h1"
    assert lean.call_args.kwargs["mode"] == "skeleton"


@pytest.mark.anyio
async def test_skeleton_check_missing_scaffold_is_decomposer_failure(tmp_path):
    """A no-scaffold prover plan must revise/fail, not continue as a fake single-goal run."""
    with patch.object(settings, "tasks_dir", tmp_path):
        task_dir = tmp_path / "missing_scaffold"
        task_dir.mkdir(parents=True)
        (task_dir / "plan.md").write_text("---\ncontext_brief: prose only\n---\n", encoding="utf-8")
        check = WorkflowNode(
            id="skeleton_check", kind="native", handler="skeleton_check", upstream=["decompose"]
        )
        ctx = NativeNodeCtx(
            task_id="missing_scaffold",
            node=check,
            request="Prove that True.",
            progress_callback=None,
        )
        res = await skeleton_check_handler(ctx)

    assert res["ok"] is False
    assert res["errors"] == ["no scaffold in plan"]


@pytest.mark.anyio
async def test_skeleton_check_verifier_timeout_is_terminal_inconclusive(tmp_path):
    plan_md = """---
task_id: verifier_timeout
task_type: code
scaffold: |
  have h1 : True := sorry
  exact h1
options:
  - id: a
    summary: direct
    subtasks:
      - id: h1
        description: prove target
        lean_type: "True"
selected_option: a
---
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        task_dir = tmp_path / "verifier_timeout"
        task_dir.mkdir(parents=True)
        (task_dir / "plan.md").write_text(plan_md, encoding="utf-8")
        check = WorkflowNode(
            id="skeleton_check", kind="native", handler="skeleton_check", upstream=["decompose"]
        )
        ctx = NativeNodeCtx(
            task_id="verifier_timeout",
            node=check,
            request="Prove that True.",
            progress_callback=None,
        )
        with patch("hyperion.crews.lean_handlers.verify_lean", return_value={
            "ok": False,
            "errors": ["lean verifier unavailable: timed out"],
            "infra_ok": False,
        }):
            res = await skeleton_check_handler(ctx)

        skeleton_ok = context_get("verifier_timeout", "skeleton_ok")

    assert res["ok"] is False
    assert res["infra_ok"] is False
    assert res["error_code"] == "verifier_timeout"
    assert skeleton_ok is None


@pytest.mark.anyio
async def test_formal_ingest_stages_structured_decompose_request(tmp_path):
    request = """import Mathlib

open Real Nat Topology

theorem foo (y : ℂ) : 7 * y = 7 * y := by
  sorry"""
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = NativeNodeCtx(
            task_id="formal_ingest_case",
            node=WorkflowNode(id="formal_ingest", kind="native", handler="formal_ingest", upstream=[]),
            request=request,
            progress_callback=None,
        )

        res = await formal_ingest_handler(ctx)
        formal_goal = context_get("formal_ingest_case", "formal_goal")
        decompose_request = context_get("formal_ingest_case", "decompose_request")

    assert res["ingested"] is True
    assert formal_goal == "7 * y = 7 * y"
    assert "formal_signature:" in decompose_request
    assert "local_context:" in decompose_request
    assert "y : ℂ" in decompose_request
    assert "import Mathlib" not in decompose_request


@pytest.mark.anyio
async def test_skeleton_check_threads_formal_context_into_native_subgoal(tmp_path):
    plan_md = """---
task_id: threaded_ctx
task_type: code
scaffold: |
  intro y
  have h1 : 7 * y = 7 * y := sorry
  exact h1
options:
  - id: a
    summary: bad context leak
    subtasks:
      - id: h1
        description: prove local goal
        lean_type: "7 * y = 7 * y"
selected_option: a
---
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "threaded_ctx").mkdir(parents=True, exist_ok=True)
        (tmp_path / "threaded_ctx" / "plan.md").write_text(plan_md, encoding="utf-8")
        context_put("threaded_ctx", "formal_statement_ingestion", {
            "preamble": "import Mathlib",
            "header": "theorem foo (y : ℂ)",
            "goal": "7 * y = 7 * y",
            "local_context": [{"names": ["y"], "type": "ℂ", "raw": "(y : ℂ)"}],
        })
        ctx = NativeNodeCtx(
            task_id="threaded_ctx",
            node=WorkflowNode(id="skeleton_check", kind="native", handler="skeleton_check", upstream=[]),
            request="ignored",
            progress_callback=None,
        )

        with mock_lean(ok=True, targets=_VERIFY_TARGET) as lean:
            res = await skeleton_check_handler(ctx)
        hits = context_get("threaded_ctx", "subgoal_unbound_context")

    assert res["ok"] is True
    assert hits == []
    source = lean.call_args.args[0]
    assert "intro y" not in source
    assert source == (
        "import Mathlib\n\n"
        "theorem foo (y : ℂ) :\n"
        "  7 * y = 7 * y := by\n"
        "  have h1 : 7 * y = 7 * y := sorry\n"
        "  exact h1"
    )


@pytest.mark.anyio
async def test_skeleton_check_uses_native_conjunction_closer_over_scaffold_text(tmp_path):
    plan_md = """---
task_id: native_close
task_type: code
scaffold: |
  have h1 : P := sorry
  have h2 : Q := sorry
  exact h2
options:
  - id: a
    summary: conjunction pieces
    subtasks:
      - id: h1
        description: prove left
        lean_type: "P"
      - id: h2
        description: prove right
        lean_type: "Q"
selected_option: a
---
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "native_close").mkdir(parents=True, exist_ok=True)
        (tmp_path / "native_close" / "plan.md").write_text(plan_md, encoding="utf-8")
        ctx = NativeNodeCtx(
            task_id="native_close",
            node=WorkflowNode(id="skeleton_check", kind="native", handler="skeleton_check", upstream=[]),
            request="Prove that P ∧ Q.",
            progress_callback=None,
        )

        with mock_lean(ok=True, targets=_VERIFY_TARGET) as lean:
            res = await skeleton_check_handler(ctx)

    assert res["ok"] is True
    assert lean.call_args.args[0] == (
        "example : P ∧ Q := by\n"
        "  have h1 : P := sorry\n"
        "  have h2 : Q := sorry\n"
        "  exact ⟨h1, h2⟩"
    )


def test_scaffold_lean3_trailing_commas_are_scrubbed():
    """A decomposer scaffold with Lean-3 ``:= sorry,`` tactic separators is scrubbed to a
    valid Lean 4 have-chain before skeleton/bank (the kernel rejects the comma form)."""
    from hyperion.crews.lean_handlers import _scaffold_as_command, _sanitize_scaffold

    scaffold = (
        "example : 18 - 7 + 4 = 15 := by\n"
        "  have h1 : 18 - 7 = 11 := sorry,\n"
        "  have h2 : 11 + 4 = 15 := sorry,\n"
        "  exact h2\n"
    )
    out = _scaffold_as_command(scaffold, "18 - 7 + 4 = 15")
    assert "," not in out
    assert "have h1 : 18 - 7 = 11 := sorry" in out
    # Commas inside terms/types (mid-line) are preserved; clean scaffolds pass through.
    keep = "have h : a = b := ⟨x, y⟩\nexact h"
    assert _sanitize_scaffold(keep) == keep


def test_scaffold_fragile_cast_closing_is_canonicalized():
    """A decomposer's over-clever ``▸``-cast closing is rewritten to ``exact <last_have>``;
    clean chain/conjunction closings pass through untouched."""
    from hyperion.crews.lean_handlers import _sanitize_scaffold

    fragile = (
        "have h1 : 20 - 5 = 15 := sorry\n"
        "have h2 : 15 + 3 = 18 := sorry\n"
        "exact h2.trans (h1.symm ▸ rfl)\n"
    )
    out = _sanitize_scaffold(fragile)
    assert "▸" not in out
    assert out.splitlines()[-1] == "exact h2"
    # Idempotent: a second pass is a no-op.
    assert _sanitize_scaffold(out) == out

    # Indentation on the closing line is preserved (body-of-``by`` scaffolds are indented).
    indented = (
        "  have h1 : 20 - 5 = 15 := sorry\n"
        "  have h2 : 15 + 3 = 18 := sorry\n"
        "  exact h2.trans (h1.symm ▸ rfl)\n"
    )
    assert _sanitize_scaffold(indented).splitlines()[-1] == "  exact h2"

    # ``.trans`` without a ``▸`` is the same fragile chain-close family.
    trans = (
        "example : (20 - 5) + 3 = 18 := by\n"
        "  have h1 : 20 - 5 = 15 := sorry\n"
        "  have h2 : 15 + 3 = 18 := sorry\n"
        "  exact h2.trans h1.symm\n"
    )
    assert _sanitize_scaffold(trans).splitlines()[-1] == "  exact h2"

    # Clean chain close and ``And.intro`` conjunction close carry no fragile marker — untouched.
    clean_chain = "have h1 : 20 - 5 = 15 := sorry\nhave h2 : 15 + 3 = 18 := sorry\nexact h2"
    assert _sanitize_scaffold(clean_chain) == clean_chain
    conj = "have h1 : a := sorry\nhave h2 : b := sorry\nexact ⟨h1, h2⟩"
    assert _sanitize_scaffold(conj) == conj


def test_sanitize_lean_source_scrubs_named_example_and_mathlibisms():
    """A synthesized proof named ``example`` (a keyword, not an identifier) is rewritten to
    the valid anonymous ``example : T := p``; imports and ``lemma`` are also scrubbed."""
    from hyperion.crews.lean_handlers import _sanitize_lean_source

    # ``lemma example : T := p`` and ``theorem example : T := p`` both → anonymous example.
    assert _sanitize_lean_source("lemma example : 20 - 5 = 15 := rfl") == "example : 20 - 5 = 15 := rfl"
    assert _sanitize_lean_source("theorem example : a = a := rfl") == "example : a = a := rfl"
    # A genuinely-named decl keeps its name; bare ``lemma`` → ``theorem``.
    assert _sanitize_lean_source("lemma foo : a = a := rfl") == "theorem foo : a = a := rfl"
    # Import lines are stripped; a clean anonymous example passes through; idempotent.
    out = _sanitize_lean_source("import Mathlib\nlemma example : True := trivial")
    assert out == "example : True := trivial"
    assert _sanitize_lean_source(out) == out


def test_sanitize_lean_source_preserves_mathlib_imports_for_mathlib_profile():
    """Mathlib-profile synthesis must keep imports while retaining parser-safe rewrites."""
    from hyperion.crews.lean_handlers import _sanitize_lean_source

    out = _sanitize_lean_source(
        "import Mathlib\nlemma example : True := trivial",
        profile="mathlib",
    )
    assert out == "import Mathlib\nexample : True := trivial"
    assert _sanitize_lean_source(out, profile="mathlib") == out


def test_synthesize_instruction_mentions_mathlib_profile(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "mathlib_synth").mkdir(parents=True, exist_ok=True)
        (tmp_path / "mathlib_synth" / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        context_put("mathlib_synth", "lean_profile", "mathlib")
        node = WorkflowNode(
            id="synthesize__h1",
            kind="work",
            agent="lemma_synthesizer",
            instruction="h1",
            upstream=["skeleton_check"],
        )

        prompt = runner._synthesize_instruction("mathlib_synth", node, "Prove in Lean 4 that P.")

    assert "The verifier profile is `mathlib`" in prompt
    assert "you may include `import Mathlib`" in prompt


def test_synthesize_instruction_falls_back_to_ingested_formal_goal(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "mathlib_no_options").mkdir(parents=True, exist_ok=True)
        (tmp_path / "mathlib_no_options" / "plan.md").write_text(
            "---\ntask_id: mathlib_no_options\ntask_type: code\nscaffold: |\n  exact rfl\n---\n",
            encoding="utf-8",
        )
        context_put("mathlib_no_options", "lean_profile", "mathlib")
        context_put("mathlib_no_options", "formal_goal", "7 = 7")
        node = WorkflowNode(
            id="synthesize",
            kind="work",
            agent="lemma_synthesizer",
            upstream=["skeleton_check"],
        )

        prompt = runner._synthesize_instruction(
            "mathlib_no_options",
            node,
            "import Mathlib\n\ntheorem seven_eq : 7 = 7 := by\n  sorry",
        )

    assert "    7 = 7\n" in prompt
    assert "theorem seven_eq" not in prompt


def test_synthesize_instruction_quantifies_formal_locals_for_subgoal(tmp_path):
    plan_md = """---
task_id: threaded_synth
task_type: code
options:
  - id: a
    summary: local goal
    subtasks:
      - id: h1
        description: prove local polynomial identity
        lean_type: "7 * y = 7 * y"
selected_option: a
---
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "threaded_synth").mkdir(parents=True, exist_ok=True)
        (tmp_path / "threaded_synth" / "plan.md").write_text(plan_md, encoding="utf-8")
        context_put("threaded_synth", "formal_statement_ingestion", {
            "header": "theorem foo (y : ℂ)",
            "goal": "7 * y = 7 * y",
            "local_context": [{"names": ["y"], "type": "ℂ", "raw": "(y : ℂ)"}],
        })
        node = WorkflowNode(
            id="synthesize__h1",
            kind="work",
            agent="lemma_synthesizer",
            instruction="h1",
            upstream=["skeleton_check"],
        )

        prompt = runner._synthesize_instruction("threaded_synth", node, "ignored")

    assert "    ∀ (y : ℂ), 7 * y = 7 * y\n" in prompt


def test_plan_task_mentions_active_verifier_profile():
    calls = []

    def fake_task(**kwargs):
        calls.append(kwargs)
        return kwargs

    with patch.object(runner, "Task", side_effect=fake_task):
        task = runner._plan_task(
            "Prove in Lean 4 that ∀ (y : ℂ), 7 * (3 * y + 2) = 21 * y + 14.",
            agent=MagicMock(),
            lean_profile="mathlib",
        )

    assert task is calls[0]
    assert "Verifier profile: mathlib" in task["description"]
    assert "Mathlib imports" in task["description"]


@pytest.mark.anyio
async def test_shipped_lean_prove_mocked_workflow_runs_with_native_decompose(tmp_path):
    """The shipped workflow uses the decomposer plan node before native prover stages."""

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        if stage == "decompose":
            base = settings.tasks_dir / task_id
            base.mkdir(parents=True, exist_ok=True)
            (base / "plan.md").write_text(
                "---\n"
                f"task_id: {task_id}\n"
                "task_type: code\n"
                "selected_option: a\n"
                "scaffold: |\n"
                "  have h1 : True := sorry\n"
                "  exact h1\n"
                "options:\n"
                "  - id: a\n"
                "    summary: direct\n"
                "    subtasks:\n"
                "      - id: h1\n"
                "        description: prove True\n"
                "        lean_type: \"True\"\n"
                "---\n",
                encoding="utf-8",
            )
        elif stage == "synthesize":
            context_put(task_id, "candidate_b", {
                "source": "theorem t_h1 : True := trivial",
                "statement": "theorem t_h1 : True",
                "proof_term": "trivial",
                "origin": "synthesize",
                "lean_type": "True",
            })

    store = MagicMock(return_value={"ok": True, "id": "pt", "error": None})

    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "load_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_node_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=_fake_stage), \
         patch.object(lean_handlers, "retrieve_applicable_lemmas", lambda goal, **k: []), \
         patch.object(lean_handlers, "propose_abstraction", AsyncMock(return_value=[])), \
         patch("hyperion.memory.lemma_bank.store_lemma", store), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()), \
         mock_lean(ok=True, targets=_VERIFY_TARGET):
        result = await runner.run_task("native_full", "Prove that True.", workflow="lean-prove")

    assert result["status"] == "done", result
    text = (tmp_path / "native_full" / "artifacts" / "result.lean").read_text(encoding="utf-8")
    assert text == "example : True := by\n  have h1 : True := trivial\n  exact h1"
    assert store.call_count == 1


@pytest.mark.anyio
async def test_shipped_workflow_escalates_stall_and_banks_concept(tmp_path):
    """The registered definition-synthesis handlers are reachable from the shipped DAG:
    normal verify stalls, the concept branch accepts, bank_concept stages Path C, and the
    final bank assembles through that proof."""

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        if stage == "decompose":
            base = settings.tasks_dir / task_id
            base.mkdir(parents=True, exist_ok=True)
            (base / "plan.md").write_text(
                "---\n"
                f"task_id: {task_id}\n"
                "task_type: code\n"
                "selected_option: a\n"
                "scaffold: |\n"
                "  have h1 : True := sorry\n"
                "  exact h1\n"
                "options:\n"
                "  - id: a\n"
                "    summary: direct\n"
                "    subtasks:\n"
                "      - id: h1\n"
                "        description: prove True\n"
                "        lean_type: \"True\"\n"
                "---\n",
                encoding="utf-8",
            )
        elif stage == "synthesize":
            context_put(task_id, "candidate_b", {
                "source": "theorem t_h1 : True := by sorry",
                "statement": "theorem t_h1 : True",
                "proof_term": "by sorry",
                "origin": "synthesize",
                "lean_type": "True",
            })

    def _won(src="theorem t : True := by trivial"):
        return ProofOutcome(
            closed=True,
            source=src,
            weak_source=src,
            proof_term="by trivial",
            repair_iters=0,
            axioms=[],
            axioms_clean=True,
        )

    lost = ProofOutcome(closed=False, source=None, weak_source=None, proof_term=None,
                        repair_iters=3, axioms=[], axioms_clean=None)
    concept = {
        "definition": {"name": "Useful", "source": "def Useful (p : Prop) : Prop := p"},
        "bridges": [{"name": "Useful.intro", "source": "theorem Useful.intro : Useful True := by trivial",
                     "lean_type": "Useful True", "statement": "Useful.intro : Useful True"}],
    }
    proof_through_concept = "theorem ablation_target : True := by trivial"
    prove = AsyncMock(side_effect=[lost, _won(), _won(proof_through_concept), lost])
    lemma_store = MagicMock(return_value={"ok": True, "id": "lemma", "error": None})
    concept_store = MagicMock(return_value={"ok": True, "id": "concept", "error": None})

    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "load_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_node_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=_fake_stage), \
         patch.object(lean_handlers, "retrieve_applicable_lemmas", lambda goal, **k: []), \
         patch.object(lean_handlers, "propose_definition", AsyncMock(return_value=[concept])), \
         patch.object(lean_handlers, "prove_proposition", prove), \
         patch.object(lean_handlers, "propose_abstraction", AsyncMock(return_value=[])), \
         patch("hyperion.memory.lemma_bank.store_lemma", lemma_store), \
         patch("hyperion.memory.concept_bank.store_concept", concept_store), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()), \
         mock_lean(ok=True, targets=_VERIFY_TARGET):
        result = await runner.run_task("native_escalate", "Prove that True.", workflow="lean-prove")

    assert result["status"] == "done", result
    with patch.object(settings, "tasks_dir", tmp_path):
        assert context_get("native_escalate", "escalated:h1") is True
        accepted = context_get("native_escalate", "accepted_concept:h1")
        assert accepted["concept_id"]
        discharged = context_get("native_escalate", "discharged:h1")
        assert discharged["path"] == "C"
    concept_store.assert_called_once()
    assert lemma_store.call_count == 1
    text = (tmp_path / "native_escalate" / "artifacts" / "result.lean").read_text(encoding="utf-8")
    assert "sorry" not in text


@pytest.mark.anyio
async def test_skeleton_failure_revises_decomposer_before_sourcing(tmp_path):
    """A bad formalization loops back to decompose instead of falling through to Path A/B."""
    calls: list[str] = []

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        calls.append(stage)
        if stage == "decompose":
            bad = calls.count("decompose") == 1
            lean_type = "not Lean English prompt" if bad else "True"
            base = settings.tasks_dir / task_id
            base.mkdir(parents=True, exist_ok=True)
            (base / "plan.md").write_text(
                "---\n"
                f"task_id: {task_id}\n"
                "task_type: code\n"
                "selected_option: a\n"
                "scaffold: |\n"
                f"  have h1 : {lean_type} := sorry\n"
                "  exact h1\n"
                "options:\n"
                "  - id: a\n"
                "    summary: direct\n"
                "    subtasks:\n"
                "      - id: h1\n"
                "        description: prove target\n"
                f"        lean_type: {lean_type!r}\n"
                "---\n",
                encoding="utf-8",
            )
        elif stage == "synthesize":
            context_put(task_id, "candidate_b", {
                "source": "theorem t_h1 : True := trivial",
                "statement": "theorem t_h1 : True",
                "proof_term": "trivial",
                "origin": "synthesize",
            })

    def _verify(src, mode="full", **kwargs):
        if "not Lean English prompt" in src:
            return {"ok": False, "errors": ["bad formalization"], "infra_ok": True}
        return {"ok": True, "errors": [], "infra_ok": True}

    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "load_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_node_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=_fake_stage), \
         patch.object(lean_handlers, "verify_lean", _verify), \
         patch.object(lean_handlers, "retrieve_applicable_lemmas", lambda goal, **k: []), \
         patch.object(lean_handlers, "propose_abstraction", AsyncMock(return_value=[])), \
         patch("hyperion.memory.lemma_bank.store_lemma", MagicMock(return_value={"ok": True, "id": "pt", "error": None})), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()):
        result = await runner.run_task("revise_skeleton", "word problem", workflow="lean-prove")

    assert result["status"] == "done", result
    assert calls[:3] == ["decompose", "decompose", "synthesize"]
    text = (tmp_path / "revise_skeleton" / "artifacts" / "result.lean").read_text(encoding="utf-8")
    assert "not Lean English prompt" not in text


@pytest.mark.anyio
async def test_skeleton_verifier_timeout_fails_without_decomposer_revision(tmp_path):
    calls = []

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        calls.append(stage)
        if stage == "decompose":
            base = settings.tasks_dir / task_id
            base.mkdir(parents=True, exist_ok=True)
            (base / "plan.md").write_text(
                "---\n"
                f"task_id: {task_id}\n"
                "task_type: code\n"
                "selected_option: a\n"
                "scaffold: |\n"
                "  have h1 : True := sorry\n"
                "  exact h1\n"
                "options:\n"
                "  - id: a\n"
                "    summary: direct\n"
                "    subtasks:\n"
                "      - id: h1\n"
                "        description: prove target\n"
                "        lean_type: 'True'\n"
                "---\n",
                encoding="utf-8",
            )

    def _timeout(_src, mode="full", **kwargs):
        return {
            "ok": False,
            "errors": ["lean verifier unavailable: timed out"],
            "infra_ok": False,
        }

    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "load_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_node_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=_fake_stage), \
         patch.object(lean_handlers, "verify_lean", _timeout), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()):
        result = await runner.run_task("timeout_skeleton", "word problem", workflow="lean-prove")

    assert result["status"] == "failed"
    assert result["error"].startswith("verifier_timeout:")
    assert calls == ["decompose"]


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
async def test_nonconverging_repair_stalls_at_cap(tmp_path):
    """A repair agent that never converges (kernel always fails) hits
    ``cap_repair_iters`` exactly and records a structured stall instead of spinning."""
    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(settings, "cap_repair_iters", 3):
        ctx = _verify_ctx("v2")
        context_put("v2", "candidate_a:sg", None)
        context_put("v2", "candidate_b:sg", {"source": "b_src", "proof_term": "pb", "statement": "sb"})

        repair = AsyncMock(return_value="example : G := by sorry")
        with patch.object(lean_handlers, "propose_repair", repair), \
             mock_lean(results=[{"ok": False}], targets=_VERIFY_TARGET):
            res = await verify_handler(ctx)

        # One proposal per iteration, capped — no more, no fewer.
        assert res["ok"] is False and res["stalled"] is True
        assert repair.await_count == 3
        decision = context_get("v2", "verify_decision:sg")
        assert decision["repair_iters"] == 3
        assert decision["winner_path"] is None
        assert context_get("v2", "stall_errors:sg")


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


def test_synthesized_candidate_includes_retrieved_concept_context(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        ctx = _verify_ctx("concept-reuse")
        context_put("concept-reuse", "candidate_b:sg", {
            "source": "theorem t : Useful True := by trivial",
            "statement": "t : Useful True",
            "proof_term": "by trivial",
        })
        context_put("concept-reuse", "concept_context:sg", [{
            "definition": {"name": "Useful", "source": "def Useful (p : Prop) : Prop := p"},
            "bridges": [{"source": "theorem Useful.intro : Useful True := by trivial"}],
        }])

        cand = lean_handlers._synthesized_candidate(ctx, "sg")

    assert cand is not None
    assert cand["source"].startswith("def Useful")
    assert "theorem Useful.intro" in cand["source"]
    assert "theorem t : Useful True" in cand["source"]


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
    assert next(n for n in wf.nodes if n.id == "escalation_gate").upstream == ["verify"]
    assert next(n for n in wf.nodes if n.id == "bank").upstream == ["abstract", "bank_concept"]


@pytest.mark.anyio
async def test_escalation_gate_routes_only_stalls(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        context_put("eg", "verify_decision:sg", {"winner_path": None})
        context_put("eg", "discharged:sg", None)
        stalled = await escalation_gate_handler(NativeNodeCtx(
            task_id="eg",
            node=WorkflowNode(id="escalation_gate", kind="native", handler="escalation_gate",
                              instruction="sg"),
            request="prove G",
            progress_callback=None,
        ))
        assert stalled["escalated"] is True
        assert context_get("eg", "escalated:sg") is True

        context_put("eg2", "verify_decision:sg", {"winner_path": "A"})
        context_put("eg2", "discharged:sg", {"path": "A"})
        normal = await escalation_gate_handler(NativeNodeCtx(
            task_id="eg2",
            node=WorkflowNode(id="escalation_gate", kind="native", handler="escalation_gate",
                              instruction="sg"),
            request="prove G",
            progress_callback=None,
        ))
        assert normal["escalated"] is False
        assert context_get("eg2", "escalated:sg") is False


@pytest.mark.anyio
async def test_bank_concept_persists_and_stages_discharge(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        context_put("bc", "accepted_concept:sg", {
            "concept_id": "c1",
            "definition": {"name": "Balanced", "source": "def Balanced : Prop := True"},
            "bridges": [],
            "with_proof": "theorem ablation_target : G := by exact proofG",
            "origin": "synthesized",
            "provisional": True,
            "necessity_hits": 0,
            "times_won": 1,
        })
        store = MagicMock(return_value={"ok": True, "id": "cid", "error": None})
        ctx = NativeNodeCtx(
            task_id="bc",
            node=WorkflowNode(id="bank_concept", kind="native", handler="bank_concept",
                              instruction="sg"),
            request="prove G",
            progress_callback=None,
        )
        with patch("hyperion.memory.concept_bank.store_concept", store):
            res = await bank_concept_handler(ctx)

    assert res["ok"] is True
    store.assert_called_once()
    with patch.object(settings, "tasks_dir", tmp_path):
        discharged = context_get("bc", "discharged:sg")
    assert discharged["path"] == "C"
    assert discharged["concept_id"] == "c1"
    assert discharged["proof_term"] == "by exact proofG"


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


def test_capture_lemma_candidate_writes_namespaced_candidate(tmp_path):
    """The runner deterministically captures the tool-less synthesizer's JSON final answer
    into ``candidate_b:<sg>`` (sub-goal id from the ``instruction``/``__`` clone suffix), so
    the Path-B hand-off never depends on the agent calling a tool. Was the live h2 failure."""
    from hyperion.memory.context_store import context_get

    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "cap").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cap" / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        node = WorkflowNode(id="synthesize__h2", kind="work", agent="lemma_synthesizer",
                            instruction="h2", upstream=["skeleton_check"])
        # Agent answered with prose + the JSON object (braces inside the Lean source).
        result = SimpleNamespace(raw=(
            'Thought: done\nFinal Answer: {"source": "theorem t : 11 + 4 = 15 := by\n'
            '  rfl", "statement": "t : 11 + 4 = 15", "proof_term": "by { rfl }"}'
        ))
        runner._capture_lemma_candidate("cap", node, result)

        cand = context_get("cap", "candidate_b:h2")
        assert cand and cand["proof_term"] == "by { rfl }"
        assert cand["origin"] == "synthesize"          # defaulted by the capturer
        # Idempotent: a populated key is never overwritten.
        runner._capture_lemma_candidate("cap", node, SimpleNamespace(raw="{}"))
        assert context_get("cap", "candidate_b:h2")["proof_term"] == "by { rfl }"


@pytest.mark.anyio
async def test_shipped_single_chain_expands_per_subgoal_at_runtime(tmp_path):
    """The shipped single-chain ``lean-prove`` workflow auto-fans-out when the plan has
    >1 sub-goal: after skeleton_check passes, the runner clones the retrieve/synthesize/
    verify/compare/abstract chain per sub-goal (``expand_per_subgoal``) so BOTH h1 and h2
    are discharged and banked — the multi-``have`` scaffold the old single chain failed."""

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        base = settings.tasks_dir / task_id
        (base / "notes").mkdir(parents=True, exist_ok=True)
        (base / "artifacts").mkdir(parents=True, exist_ok=True)
        if stage == "decompose":
            (base / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        elif stage.startswith("synthesize__"):  # cloned per-sub-goal synth node
            sg = stage.split("__", 1)[1]
            # The tool-less synthesizer returns its candidate as a raw final answer (no
            # context_put); the runner's _capture_lemma_candidate must persist it itself.
            return SimpleNamespace(raw=json.dumps({
                "source": f"theorem t_{sg} : G := by trivial",
                "statement": f"theorem t_{sg} : G",
                "proof_term": f"{sg}_synth_proof",
                "origin": "synthesize",
            }))

    def _fake_retrieve(goal, **kwargs):
        if goal.strip() == "P":  # only h1 has a banked lemma; h2 falls to Path B
            return [{"statement": "lem_P", "proof_term": "lemP_proof", "lean_type": "P"}]
        return []

    store = MagicMock(return_value={"ok": True, "id": "pt", "error": None})
    abstraction = AsyncMock(return_value=[{
        "source": "theorem gen : G := by trivial", "statement": "theorem gen : G",
        "proof_term": "by trivial", "lean_type": "∀ x, G"}])

    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "load_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_node_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=_fake_stage), \
         patch.object(lean_handlers, "retrieve_applicable_lemmas", _fake_retrieve), \
         patch.object(lean_handlers, "propose_abstraction", abstraction), \
         patch("hyperion.memory.lemma_bank.store_lemma", store), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()), \
         mock_lean(ok=True, targets=_VERIFY_TARGET):
        # Real resolver — load the SHIPPED single-chain lean-prove from config.
        result = await runner.run_task("prove_exp", "prove P ∧ Q", workflow="lean-prove")

    assert result["status"] == "done", result
    # The fan-out is visible in routing: per-sub-goal clones ran.
    agents = result["routing"]["selected_agents"]
    assert "verify__h1" in agents and "verify__h2" in agents, agents
    text = (tmp_path / "prove_exp" / "artifacts" / "result.lean").read_text(encoding="utf-8")
    assert "sorry" not in text
    assert "lemP_proof" in text        # h1 via Path A
    assert "h2_synth_proof" in text    # h2 via Path B
    assert store.call_count == 2       # both sub-goals banked


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


@pytest.mark.anyio
async def test_bank_skips_lemma_writes_in_eval_no_write_mode(tmp_path):
    """Dev/test evaluation still verifies result.lean but must not mutate the lemma bank."""
    from hyperion.crews.lean_handlers import bank_handler

    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "prove_nowrite").mkdir(parents=True, exist_ok=True)
        (tmp_path / "prove_nowrite" / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        context_put("prove_nowrite", "learning_writes_enabled", False)
        context_put("prove_nowrite", "discharged:h1",
                    {"proof_term": "lemP_proof", "statement": "lem_P", "path": "A", "lean_type": "P"})
        context_put("prove_nowrite", "discharged:h2",
                    {"proof_term": "h2_synth_proof", "statement": "t_h2", "path": "B", "lean_type": "Q"})
        node = WorkflowNode(id="bank", kind="native", handler="bank", upstream=[])
        ctx = NativeNodeCtx(task_id="prove_nowrite", node=node, request="prove P ∧ Q", progress_callback=None)

        store = MagicMock(return_value={"ok": True, "id": "pt", "error": None})
        with patch("hyperion.memory.lemma_bank.store_lemma", store), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            res = await bank_handler(ctx)

        assert res["ok"] is True
        assert res["bank_writes_enabled"] is False
        assert res["n_banked"] == 0
        store.assert_not_called()
        assert (tmp_path / "prove_nowrite" / "artifacts" / "result.lean").exists()


@pytest.mark.anyio
async def test_bank_wraps_formal_statement_for_final_verify(tmp_path):
    plan_md = """---
task_id: formal_bank
task_type: code
scaffold: |
  have h1 : 7 = 7 := sorry
  exact h1
options:
  - id: a
    summary: direct
    subtasks:
      - id: h1
        description: prove target
        lean_type: "7 = 7"
selected_option: a
---
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "formal_bank").mkdir(parents=True, exist_ok=True)
        (tmp_path / "formal_bank" / "plan.md").write_text(plan_md, encoding="utf-8")
        context_put("formal_bank", "learning_writes_enabled", False)
        context_put("formal_bank", "formal_statement_ingestion", {
            "preamble": "import Mathlib",
            "header": "theorem seven_eq",
            "goal": "7 = 7",
            "local_context": [],
        })
        context_put("formal_bank", "discharged:h1", {
            "proof_term": "rfl",
            "statement": "theorem h : 7 = 7",
            "path": "B",
        })
        node = WorkflowNode(id="bank", kind="native", handler="bank", upstream=[])
        ctx = NativeNodeCtx(task_id="formal_bank", node=node, request="ignored", progress_callback=None)

        with mock_lean(ok=True, targets=_VERIFY_TARGET) as lean:
            await bank_handler(ctx)

    source = lean.call_args.args[0]
    assert source.startswith("import Mathlib\n\ntheorem seven_eq :\n  7 = 7 := by")
    assert "example :" not in source
    assert "exact h1" in source


@pytest.mark.anyio
async def test_bank_instantiates_threaded_subgoal_proof_inside_formal_statement(tmp_path):
    plan_md = """---
task_id: threaded_bank
task_type: code
options:
  - id: a
    summary: local goal
    subtasks:
      - id: h1
        description: prove local polynomial identity
        lean_type: "7 * y = 7 * y"
selected_option: a
---
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "threaded_bank").mkdir(parents=True, exist_ok=True)
        (tmp_path / "threaded_bank" / "plan.md").write_text(plan_md, encoding="utf-8")
        context_put("threaded_bank", "learning_writes_enabled", False)
        context_put("threaded_bank", "formal_statement_ingestion", {
            "preamble": "import Mathlib",
            "header": "theorem foo (y : ℂ)",
            "goal": "7 * y = 7 * y",
            "local_context": [{"names": ["y"], "type": "ℂ", "raw": "(y : ℂ)"}],
        })
        context_put("threaded_bank", "discharged:h1", {
            "proof_term": "by intro y; rfl",
            "statement": "theorem h : ∀ (y : ℂ), 7 * y = 7 * y",
            "path": "B",
        })
        node = WorkflowNode(id="bank", kind="native", handler="bank", upstream=[])
        ctx = NativeNodeCtx(task_id="threaded_bank", node=node, request="ignored", progress_callback=None)

        with mock_lean(ok=True, targets=_VERIFY_TARGET) as lean:
            await bank_handler(ctx)

    source = lean.call_args.args[0]
    assert source.startswith("import Mathlib\n\ntheorem foo (y : ℂ) :\n  7 * y = 7 * y := by")
    assert "have h1 : 7 * y = 7 * y := by exact (by intro y; rfl) y" in source
    assert "exact h1" in source


@pytest.mark.anyio
async def test_bank_rejects_invalid_final_assembly(tmp_path):
    """A node may discharge a subgoal, but the assembled result.lean is the final gate."""
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "bad_final").mkdir(parents=True, exist_ok=True)
        (tmp_path / "bad_final" / "plan.md").write_text(
            "---\n"
            "task_id: bad_final\n"
            "task_type: code\n"
            "selected_option: a\n"
            "scaffold: |\n"
            "  have h1 : not Lean English := sorry\n"
            "  exact h1\n"
            "options:\n"
            "  - id: a\n"
            "    summary: bad\n"
            "    subtasks:\n"
            "      - id: h1\n"
            "        description: bad\n"
            "        lean_type: \"not Lean English\"\n"
            "---\n",
            encoding="utf-8",
        )
        context_put("bad_final", "discharged:h1",
                    {"proof_term": "trivial", "statement": "bad", "path": "B"})
        ctx = NativeNodeCtx(
            task_id="bad_final",
            node=WorkflowNode(id="bank", kind="native", handler="bank", upstream=[]),
            request="word problem",
            progress_callback=None,
        )

        with mock_lean(ok=False, targets=_VERIFY_TARGET):
            with pytest.raises(ProofFailed):
                await bank_handler(ctx)

        final = context_get("bad_final", "final_verify")
        assert final["ok"] is False


# ---------------------------------------------------------------------------
# Bank assembly — a repair winner's full-declaration proof_term is reduced to a
# bare term that fits a `have … := <here>` hole (Post-work bank-hardening finding)
# ---------------------------------------------------------------------------


def test_assemble_reduces_repair_winner_to_bare_proof():
    from hyperion.crews.lean_handlers import _assemble, _bare_proof_term
    from hyperion.crews.plan_contract import Subtask

    # A repair winner carries the WHOLE declaration in proof_term.
    repair_win = {"proof_term": "theorem t : R := by exact r_proof",
                  "source": "theorem t : R := by exact r_proof"}
    assert _bare_proof_term(repair_win) == "by exact r_proof"
    # Bare proof terms pass through untouched (Path A / synthesis).
    assert _bare_proof_term({"proof_term": "lemP_proof"}) == "lemP_proof"
    assert _bare_proof_term({"proof_term": "fun h => h"}) == "fun h => h"
    # A leading comment line before the decl is tolerated.
    assert _bare_proof_term({"proof_term": "-- fixed\ntheorem t : R := by simp"}) == "by simp"

    scaffold = "theorem g : R := by\n  have h1 : R := sorry\n  exact h1"
    out = _assemble(scaffold, [Subtask(id="h1", lean_type="R")], {"h1": repair_win})
    assert "have h1 : R := by exact r_proof" in out
    assert "theorem t : R" not in out          # the declaration header is gone — well-formed


def test_assemble_collapses_multiline_pathA_proof_into_have_hole():
    """A multi-line Path-A winner block is collapsed to a single-line ``by …; …`` RHS so its
    inner lines don't inherit the scaffold's shallow indent and break the ``have``'s ``by``
    nesting (the P3-bare ``expected indented tactic sequence`` regression)."""
    from hyperion.crews.lean_handlers import _assemble
    from hyperion.crews.plan_contract import Subtask

    # The shape a Path-A winner stores: a multi-line tactic block.
    win = {"proof_term": "by\n  have h : 2 ^ 3 = 8 := rfl\n  first | exact h | apply h"}
    scaffold = "example : T := by\n  have h1 : 2 ^ 3 = 8 := sorry\n  exact h1"
    out = _assemble(scaffold, [Subtask(id="h1", lean_type="2 ^ 3 = 8")], {"h1": win})
    # The substituted proof occupies exactly the one ``have h1`` line — no orphaned, shallow
    # inner ``have h`` / ``first`` lines that would parse as siblings of ``have h1``.
    have_line = next(l for l in out.splitlines() if l.lstrip().startswith("have h1"))
    assert "by have h : 2 ^ 3 = 8 := rfl; first | exact h | apply h" in have_line
    assert not any(l.lstrip().startswith("first |") for l in out.splitlines())


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
    assert bank.upstream == ["abstract", "bank_concept"]


@pytest.mark.anyio
async def test_compare_increments_times_won_for_winner(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "win1").mkdir(parents=True, exist_ok=True)
        (tmp_path / "win1" / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        context_put("win1", "candidate_a:h1", {
            "id": "lemma-pt",
            "source": "a_src",
            "statement": "lem_P",
            "proof_term": "pa",
            "lean_type": "P",
            "times_won": 2,
        })
        context_put("win1", "verified_a:h1", {
            "id": "lemma-pt",
            "source": "a_src",
            "statement": "lem_P",
            "proof_term": "pa",
            "lean_type": "P",
            "path": "A",
            "times_won": 2,
        })

        with patch("hyperion.memory.lemma_bank.bump_times_won", MagicMock(return_value=3)) as bump:
            res = await compare_handler(_native_ctx("win1", "compare", "compare"))
        winner = context_get("win1", "discharged:h1")

    assert res["winner_path"] == "A"
    bump.assert_called_once()
    assert winner["times_won"] == 3


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


@pytest.mark.anyio
async def test_bank_preserves_winner_counters_on_store(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "win2").mkdir(parents=True, exist_ok=True)
        (tmp_path / "win2" / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
        context_put("win2", "discharged:h1", {
            "proof_term": "pa",
            "statement": "lem_P",
            "path": "A",
            "lean_type": "P",
            "times_retrieved": 7,
            "times_won": 4,
        })

        store = MagicMock(return_value={"ok": True, "id": "pt", "error": None})
        with patch("hyperion.memory.lemma_bank.store_lemma", store), \
             mock_lean(ok=True, targets=_VERIFY_TARGET):
            await bank_handler(_native_ctx("win2", "bank", "bank"))

    assert store.call_args.kwargs["times_retrieved"] == 7
    assert store.call_args.kwargs["times_won"] == 4


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
