"""HITL plan-gate tests (PLAN_UNIFIED.md Phase 3).

The crew stages are mocked (``_run_stage`` / ``build_agent``) so these exercise the
pause/resume control flow without any LLM call. plan.md is written to the workspace
directly to stand in for the (mocked) planner output.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyperion.crews import runner
from hyperion.crews.plan_contract import parse_plan


@pytest.fixture
def anyio_backend():
    return "asyncio"


@contextlib.contextmanager
def _mock_crew(stage_impl):
    """Patch out every CrewAI-touching helper so control flow runs LLM-free.
    ``stage_impl`` is the (async) stand-in for ``_run_stage``."""
    with patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_plan_task", MagicMock()), \
         patch.object(runner, "_work_task", MagicMock()), \
         patch.object(runner, "_synthesize_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=stage_impl) as stage:
        yield stage


def _write_plan(base, task_id, *, task_type="research", with_options=True):
    d = base / task_id
    d.mkdir(parents=True, exist_ok=True)
    opts = ""
    if with_options:
        opts = (
            "options:\n"
            "  - id: a\n    summary: shallow pass\n    subtasks:\n      - id: s1\n        description: quick scan\n"
            "  - id: b\n    summary: deep dive\n    subtasks:\n      - id: s1\n        description: thorough\n"
        )
    (d / "plan.md").write_text(
        f"---\ntask_type: {task_type}\nkeywords: [demo]\n{opts}---\n\n# Plan\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# gate() truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage,hitl,expected",
    [
        ("plan", "off", False),
        ("plan", "plan", True),
        ("plan", "full", True),
        ("work", "full", False),       # only the plan stage gates
        ("synthesize", "plan", False),
    ],
)
def test_gate_truth_table(stage, hitl, expected):
    assert runner.gate("t", stage, hitl) is expected


# ---------------------------------------------------------------------------
# run_task: pause vs. straight-through
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_task_pauses_at_plan_gate(tmp_path):
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path), \
         _mock_crew(AsyncMock()) as stage:
        _write_plan(tmp_path, "t1")
        result = await runner.run_task("t1", "do research", hitl="plan")

    assert result["status"] == "awaiting_approval"
    assert result["pending_stage"] == "research"
    assert result["pending_payload"]["revise_count"] == 0
    assert result["pending_payload"]["hitl"] == "plan"
    # Only the plan stage ran; work/synth are deferred until approval.
    assert stage.await_count == 1


@pytest.mark.anyio
async def test_run_task_straight_through_when_hitl_off(tmp_path):
    from hyperion.config import settings

    stages: list[str] = []

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        stages.append(stage)

    with patch.object(settings, "tasks_dir", tmp_path), _mock_crew(_fake_stage):
        _write_plan(tmp_path, "t2")
        result = await runner.run_task("t2", "do research", hitl="off")
        # No gate → first option auto-selected (read inside the tasks_dir patch).
        selected = parse_plan("t2").selected_option

    assert result["status"] == "done"
    assert stages == ["plan", "research", "synthesize"]
    assert selected == "a"


# ---------------------------------------------------------------------------
# resume_task: approve / reject / revise
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resume_approve_runs_work_and_synth(tmp_path):
    from hyperion.config import settings

    stages: list[str] = []

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        stages.append(stage)

    with patch.object(settings, "tasks_dir", tmp_path), _mock_crew(_fake_stage):
        _write_plan(tmp_path, "t3")
        result = await runner.resume_task("t3", "do research", "approve", chosen_option="b")
        selected = parse_plan("t3").selected_option

    assert result["status"] == "done"
    assert stages == ["research", "synthesize"]   # no re-plan on approve
    assert selected == "b"


@pytest.mark.anyio
async def test_resume_reject_fails(tmp_path):
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path), \
         _mock_crew(AsyncMock()) as stage:
        _write_plan(tmp_path, "t4")
        result = await runner.resume_task("t4", "do research", "reject")

    assert result["status"] == "failed"
    assert "rejected" in result["error"].lower()
    assert stage.await_count == 0   # nothing runs after a reject


@pytest.mark.anyio
async def test_resume_revise_repauses(tmp_path):
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path), \
         _mock_crew(AsyncMock()) as stage:
        _write_plan(tmp_path, "t5")
        result = await runner.resume_task(
            "t5", "do research", "revise", edits="add more depth", hitl="plan", revise_count=0
        )

    assert result["status"] == "awaiting_approval"
    assert result["pending_payload"]["revise_count"] == 1
    assert stage.await_count == 1   # the planner re-ran once


@pytest.mark.anyio
async def test_resume_revise_force_continues_after_budget(tmp_path):
    from hyperion.config import settings

    stages: list[str] = []

    async def _fake_stage(task_id, request, stage, agents, tasks, cb, deadline):
        stages.append(stage)

    with patch.object(settings, "tasks_dir", tmp_path), _mock_crew(_fake_stage):
        _write_plan(tmp_path, "t6")
        # revise_count already at the cap-1 → this revise exhausts the budget.
        result = await runner.resume_task(
            "t6", "do research", "revise", edits="again", hitl="plan",
            revise_count=runner._MAX_REVISIONS - 1,
        )

    assert result["status"] == "done"
    assert stages == ["plan", "research", "synthesize"]   # re-plan, then forced through
