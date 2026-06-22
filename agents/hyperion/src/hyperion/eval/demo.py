"""Offline prover demo — run sample theorems through the REAL runner and trace each stage.

This is the "kick off some problems and watch each stage perform" harness. It drives the
actual ``runner.run_task`` over a dynamically-built ``lean-prove`` workflow (decompose →
skeleton_check → retrieve ‖ synthesize → verify → compare → abstract → bank), with the two
external dependencies mocked so it runs with **no live toolchain**:

  - **Lean** — a content-aware fake ``verify_lean``: ``skeleton`` mode always elaborates
    (``sorry`` is allowed), ``full`` mode passes iff the source has no ``sorry`` and is not in
    the problem's ``fail_full`` set. This is faithful enough to exercise the real routing — a
    ``sorry``-bearing Path-B candidate genuinely fails ``full`` and triggers the repair loop.
  - **LLM** — the agent stages (``_run_stage``) are replaced by a fake that writes the plan and
    the synthesized candidate from the problem spec; ``propose_repair`` / ``propose_abstraction``
    return canned proposals. The kernel still judges every proposal (it cannot be faked).

Run it: ``./.venv/bin/uv run python -m hyperion.eval.demo``. The same workflow runs live once
the Lean sidecar + an LLM proxy are reachable — drop the mocks and point ``LEAN_URL`` at the
sidecar.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from hyperion.config import settings
from hyperion.crews import lean_handlers, runner
from hyperion.crews.workflows import WorkflowNode, WorkflowRecord
from hyperion.eval import thesis_curve
from hyperion.eval.trace import format_trace, trace_task
from hyperion.memory.context_store import context_put


@dataclass
class Problem:
    """A demo theorem and the canned stage outputs that drive it through the pipeline."""

    id: str
    request: str
    plan_md: str
    subgoal_ids: list[str]
    retrieved: dict[str, list[dict]] = field(default_factory=dict)   # goal_type -> Path-A lemmas
    synth: dict[str, dict] = field(default_factory=dict)             # sg -> candidate_b
    fail_full: set[str] = field(default_factory=set)                 # sources the kernel rejects
    repair_proposal: Optional[str] = None                            # propose_repair return
    abstraction: list[dict] = field(default_factory=list)            # propose_abstraction return
    research_mode: bool = False


def build_workflow(subgoal_ids: list[str]) -> WorkflowRecord:
    """The full Phase-5 prover DAG for N sub-goals (one chain per ``sorry``)."""
    nodes = [
        WorkflowNode(id="decompose", kind="plan", agent="decomposer", upstream=[]),
        WorkflowNode(id="skeleton_check", kind="native", handler="skeleton_check",
                     upstream=["decompose"]),
    ]
    abstract_ids = []
    for sg in subgoal_ids:
        nodes += [
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
        abstract_ids.append(f"abstract_{sg}")
    nodes.append(WorkflowNode(id="bank", kind="native", handler="bank", upstream=abstract_ids))
    return WorkflowRecord(id="lean-prove-demo", name="demo", nodes=nodes)


@contextmanager
def _mocked(problem: Problem, tasks_root: Path):
    """Patch the runner's LLM/Lean/Qdrant seams so the run is fully offline."""

    def _fake_verify(source, *, mode="full", profile="core", timeout=None):
        if mode == "skeleton":
            ok = True  # sorry elaborates in skeleton mode
        else:
            ok = ("sorry" not in source) and (source not in problem.fail_full)
        return {"ok": ok, "errors": ([] if ok else ["demo: does not close"]),
                "elaborated_term": None, "mode": mode, "profile": profile, "infra_ok": True}

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        base = settings.tasks_dir / task_id
        (base / "notes").mkdir(parents=True, exist_ok=True)
        (base / "artifacts").mkdir(parents=True, exist_ok=True)
        if stage == "decompose":
            (base / "plan.md").write_text(problem.plan_md, encoding="utf-8")
        elif stage.startswith("synth_"):
            sg = stage.split("_", 1)[1]
            if sg in problem.synth:
                context_put(task_id, f"candidate_b:{sg}", problem.synth[sg])

    def _fake_retrieve(goal, **kwargs):
        return list(problem.retrieved.get(goal.strip(), []))

    wf = build_workflow(problem.subgoal_ids)
    repair = AsyncMock(return_value=problem.repair_proposal
                       or f"-- repaired\nexample : {problem.request} := by exact proof")
    abstraction = AsyncMock(return_value=list(problem.abstraction))

    with ExitStack() as stack:
        ep = stack.enter_context
        ep(patch.object(settings, "tasks_dir", tasks_root))
        ep(patch.object(settings, "prover_research_mode", problem.research_mode))
        ep(patch.object(runner, "build_agent", MagicMock()))
        ep(patch.object(runner, "load_agent", MagicMock()))
        ep(patch.object(runner, "discover_context", MagicMock(return_value=None)))
        ep(patch.object(runner, "_node_task", MagicMock()))
        ep(patch.object(runner, "_run_stage", new=_fake_stage))
        ep(patch("hyperion.crews.workflows.resolve_workflow", new=MagicMock(return_value=wf)))
        ep(patch.object(lean_handlers, "retrieve_applicable_lemmas", _fake_retrieve))
        # Disable the deterministic closer battery: the demo's content-aware fake verifier
        # would let the battery close every sorry-free probe, skipping the very synth/repair/
        # research mechanics these samples exist to demonstrate. The battery is exercised by
        # its own tests and the real dev/train eval.
        ep(patch.object(lean_handlers, "_run_closer_battery", return_value=(None, None, [])))
        ep(patch.object(lean_handlers, "propose_repair", repair))
        ep(patch.object(lean_handlers, "propose_abstraction", abstraction))
        ep(patch("hyperion.tools.lean_verify.verify_lean", side_effect=_fake_verify))
        ep(patch("hyperion.crews.lean_handlers.verify_lean", side_effect=_fake_verify))
        ep(patch("hyperion.memory.lemma_bank.store_lemma",
                 MagicMock(return_value={"ok": True, "id": "pt", "error": None})))
        ep(patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()))
        yield


async def run_problem(problem: Problem, tasks_root: Path) -> tuple[dict, dict]:
    """Run one problem end-to-end (mocked) and return ``(run_result, stage_trace)``."""
    with _mocked(problem, tasks_root):
        result = await runner.run_task(problem.id, problem.request, workflow="lean-prove-demo")
        trace = trace_task(problem.id, request=problem.request, status=result["status"])
    return result, trace


# ---------------------------------------------------------------------------
# Sample problems — each exercises a different path through the pipeline
# ---------------------------------------------------------------------------

def _plan(task_id: str, scaffold: str, subtasks_yaml: str) -> str:
    indented = scaffold.replace("\n", "\n  ")
    return (f"---\ntask_id: {task_id}\ntask_type: code\nscaffold: |\n  {indented}\n"
            f"options:\n  - id: a\n    summary: demo\n    subtasks:\n{subtasks_yaml}---\n")


SAMPLE_PROBLEMS: list[Problem] = [
    # 1) Conjunction: h1 closed by RETRIEVAL (Path A), h2 by SYNTHESIS (Path B). DEPLOY.
    Problem(
        id="demo_conjunction",
        request="prove P ∧ Q",
        plan_md=_plan(
            "demo_conjunction",
            "theorem target : P ∧ Q := by\n  have h1 : P := sorry\n  have h2 : Q := sorry\n  exact ⟨h1, h2⟩",
            "      - id: h1\n        description: prove P\n        lean_type: \"P\"\n"
            "      - id: h2\n        description: prove Q\n        lean_type: \"Q\"\n",
        ),
        subgoal_ids=["h1", "h2"],
        retrieved={"P": [{"statement": "lem_P", "proof_term": "lemP_proof", "lean_type": "P"}]},
        synth={
            "h1": {"source": "theorem t_h1 : P := by trivial", "statement": "theorem t_h1 : P",
                   "proof_term": "h1_synth_proof", "origin": "synthesize", "lean_type": "P"},
            "h2": {"source": "theorem t_h2 : Q := by trivial", "statement": "theorem t_h2 : Q",
                   "proof_term": "h2_synth_proof", "origin": "synthesize", "lean_type": "Q"},
        },
    ),
    # 2) Repair loop: synthesized candidate has `sorry` (fails full) → repair fixes it → passes.
    Problem(
        id="demo_repair",
        request="prove R",
        plan_md=_plan(
            "demo_repair",
            "theorem target : R := by\n  have h1 : R := sorry\n  exact h1",
            "      - id: h1\n        description: prove R\n        lean_type: \"R\"\n",
        ),
        subgoal_ids=["h1"],
        retrieved={},  # no banked lemma → Path A empty, forces Path B
        synth={"h1": {"source": "theorem t_h1 : R := by sorry", "statement": "theorem t_h1 : R",
                      "proof_term": "by sorry", "origin": "synthesize", "lean_type": "R"}},
        repair_proposal="theorem t_h1 : R := by exact r_proof",  # sorry-free → kernel accepts
    ),
    # 3) RESEARCH: both paths verify → compare contest → abstractor generalizes the Path-B lemma.
    Problem(
        id="demo_research_abstract",
        request="prove S",
        plan_md=_plan(
            "demo_research_abstract",
            "theorem target : S := by\n  have h1 : S := sorry\n  exact h1",
            "      - id: h1\n        description: prove S\n        lean_type: \"S\"\n",
        ),
        subgoal_ids=["h1"],
        retrieved={"S": [{"statement": "lem_S", "proof_term": "lemS_proof", "lean_type": "S"}]},
        synth={"h1": {"source": "theorem t_h1 : S := by exact s_proof", "statement": "theorem t_h1 : S",
                      "proof_term": "s_proof", "origin": "synthesize", "lean_type": "S"}},
        abstraction=[{"source": "theorem gen {α : Type} (x : α) : x = x := rfl",
                      "statement": "theorem gen {α : Type} (x : α) : x = x",
                      "proof_term": "rfl", "lean_type": "∀ {α : Type} (x : α), x = x"}],
        research_mode=True,
    ),
]


async def _main_async() -> None:
    all_triples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for problem in SAMPLE_PROBLEMS:
            result, trace = await run_problem(problem, root)
            print("\n" + "=" * 78)
            print(format_trace(trace))
            for sg in trace["subgoals"].values():
                if sg.get("triple_log"):
                    all_triples.append(sg["triple_log"])
    print("\n" + "=" * 78)
    print(thesis_curve.format_summary(all_triples))


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
