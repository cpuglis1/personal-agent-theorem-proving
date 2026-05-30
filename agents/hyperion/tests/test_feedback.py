"""Feedback / alerts / affordance tests (PLAN_UNIFIED.md Phase 6).

File-backed channels (feedback queue, affordances, alerts) are exercised against a
patched ``tasks_dir``. The crew stages are mocked so the awaiting_input pause/resume
control flow runs without any LLM call.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyperion import alerts, feedback
from hyperion.config import settings
from hyperion.crews import runner


@pytest.fixture
def anyio_backend():
    return "asyncio"


@contextlib.contextmanager
def _mock_crew(stage_impl):
    with patch.object(runner, "build_agent", MagicMock()), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch.object(runner, "_plan_task", MagicMock()), \
         patch.object(runner, "_work_task", MagicMock()), \
         patch.object(runner, "_synthesize_task", MagicMock()), \
         patch.object(runner, "_run_stage", new=stage_impl) as stage:
        yield stage


def _write_plan(base, task_id):
    d = base / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.md").write_text(
        "---\ntask_type: research\nkeywords: [demo]\n"
        "options:\n  - id: a\n    summary: shallow\n    subtasks:\n      - id: s1\n        description: scan\n"
        "---\n\n# Plan\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Feedback queue — delivered exactly once
# ---------------------------------------------------------------------------


def test_feedback_queue_roundtrip(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        feedback.append_feedback("t1", "look at X")
        feedback.append_feedback("t1", "also Y")
        first = feedback.drain_feedback("t1")
        second = feedback.drain_feedback("t1")

    assert first == ["look at X", "also Y"]
    assert second == []  # consumed once


def test_inject_feedback_wraps_as_data(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        assert runner.inject_feedback("t1") is None  # empty queue
        feedback.append_feedback("t1", "prioritise the API section")
        block = runner.inject_feedback("t1")
        assert runner.inject_feedback("t1") is None  # drained once

    assert block is not None
    assert "data, not instructions" in block
    assert "prioritise the API section" in block


# ---------------------------------------------------------------------------
# Affordances
# ---------------------------------------------------------------------------


def test_affordance_record_and_answer(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        aff_id = feedback.record_affordance(
            "t2", {"type": "question", "prompt": "Which region?"}
        )
        pending = feedback.latest_pending_affordance("t2")
        assert pending is not None
        assert pending["id"] == aff_id
        assert pending["prompt"] == "Which region?"

        ok = feedback.answer_affordance("t2", "EU")
        assert ok is True
        assert feedback.latest_pending_affordance("t2") is None
        # The answer is pushed onto the feedback queue for the resuming stage.
        assert any("EU" in m for m in feedback.drain_feedback("t2"))


def test_ask_user_tool_records_affordance(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        tool = feedback.AskUserTool(task_id="t3")
        out = tool._run("Do you want a summary table?")
        assert "Asked the user" in out
        pending = feedback.latest_pending_affordance("t3")
    assert pending["type"] == "question"


# ---------------------------------------------------------------------------
# Alerts — exactly one per (task, kind)
# ---------------------------------------------------------------------------


def test_alert_fires_once_per_kind(tmp_path):
    alerts.reset("t4")
    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(alerts, "_push_notification", MagicMock()):
        first = alerts.emit_alert("t4", "tool-loop", "near cap")
        second = alerts.emit_alert("t4", "tool-loop", "near cap again")

    assert first is True
    assert second is False  # deduped
    body = (tmp_path / "t4" / "alerts.md").read_text(encoding="utf-8")
    assert "tool-loop" in body


def test_alert_disabled_by_setting(tmp_path):
    alerts.reset("t5")
    with patch.object(settings, "tasks_dir", tmp_path), \
         patch.object(settings, "hyperion_hitl_alerts", "off"), \
         patch.object(alerts, "_push_notification", MagicMock()):
        fired = alerts.emit_alert("t5", "wall-budget", "slow")
    assert fired is False
    assert not (tmp_path / "t5" / "alerts.md").exists()


# ---------------------------------------------------------------------------
# run_task pauses on a planner question, resumes on the answer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_task_pauses_on_affordance(tmp_path):
    async def _stage(task_id, request, stage, agents, tasks, cb, deadline):
        # The (mocked) planner "asks" a question during the plan stage.
        if stage == "plan":
            feedback.record_affordance(task_id, {"type": "question", "prompt": "Scope?"})

    with patch.object(settings, "tasks_dir", tmp_path), _mock_crew(_stage):
        _write_plan(tmp_path, "t6")
        result = await runner.run_task("t6", "vague request", hitl="off")

    assert result["status"] == "awaiting_input"
    assert result["pending_stage"] == "plan"


@pytest.mark.anyio
async def test_resume_after_answer_runs_through(tmp_path):
    stages: list[str] = []

    async def _stage(task_id, request, stage, agents, tasks, cb, deadline):
        stages.append(stage)

    with patch.object(settings, "tasks_dir", tmp_path), _mock_crew(_stage):
        _write_plan(tmp_path, "t7")
        # The answer arrives as a revise with edits; no pending affordance remains.
        result = await runner.resume_task(
            "t7", "vague request", "revise", edits="Scope is EU only", hitl="off"
        )

    assert result["status"] == "done"
    assert stages == ["plan", "research", "synthesize"]
