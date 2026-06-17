"""Native-node seam tests (build-plan Phase 1, §1).

The one structural runner change of the whole build: a ``kind == "native"`` node that
dispatches to a registered plain-Python handler, exactly parallel to how a
``subworkflow`` node dispatches to a child workflow. These tests prove:

  - Schema: ``validate_workflow``'s exactly-one-of rule extends to native nodes
    (native ⇒ handler set, agent/workflow unset; non-native ⇒ handler unset), and
    dangling handler refs are caught when the known-handler set is supplied.
  - Dispatch: ``run_native_node`` routes to the registered handler; unknown handlers raise.
  - Orchestration: a one-node native workflow runs end-to-end in the runner, records
    routing, and writes to the blackboard — with NO agents, NO LLM, NO Lean.

The existing subworkflow/agent dispatch is left unchanged; the full suite is the
regression net for that (additive change).
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyperion.crews import runner
from hyperion.crews.native import (
    NATIVE_HANDLERS,
    NativeNodeCtx,
    get_native_handler,
    register_native_handler,
    run_native_node,
)
from hyperion.crews.workflows import WorkflowNode, WorkflowRecord, validate_workflow


# ---------------------------------------------------------------------------
# Schema validation: native exactly-one-of (mirrors the subworkflow rule)
# ---------------------------------------------------------------------------


def test_native_node_requires_handler():
    rec = WorkflowRecord(
        id="wf", name="wf",
        nodes=[WorkflowNode(id="n", kind="native", upstream=[])],
    )
    with pytest.raises(ValueError, match="must set 'handler'"):
        validate_workflow(rec, known_agent_ids=set())


def test_native_node_must_not_set_agent():
    rec = WorkflowRecord(
        id="wf", name="wf",
        nodes=[WorkflowNode(id="n", kind="native", handler="echo", agent="planner", upstream=[])],
    )
    with pytest.raises(ValueError, match="must not also set 'agent'"):
        validate_workflow(rec, known_agent_ids={"planner"})


def test_native_node_must_not_set_workflow():
    rec = WorkflowRecord(
        id="wf", name="wf",
        nodes=[WorkflowNode(id="n", kind="native", handler="echo", workflow="child", upstream=[])],
    )
    with pytest.raises(ValueError, match="must not also set 'workflow'"):
        validate_workflow(rec, known_agent_ids=set(), known_workflow_ids={"child"})


def test_non_native_node_must_not_set_handler():
    rec = WorkflowRecord(
        id="wf", name="wf",
        nodes=[WorkflowNode(id="n", kind="work", agent="planner", handler="echo", upstream=[])],
    )
    with pytest.raises(ValueError, match="sets 'handler' but kind"):
        validate_workflow(rec, known_agent_ids={"planner"})


def test_native_node_unknown_handler_rejected_when_set_known():
    rec = WorkflowRecord(
        id="wf", name="wf",
        nodes=[WorkflowNode(id="n", kind="native", handler="nope", upstream=[])],
    )
    with pytest.raises(ValueError, match="unknown native handler"):
        validate_workflow(rec, known_agent_ids=set(), known_handler_ids={"echo"})


def test_valid_native_node_passes():
    rec = WorkflowRecord(
        id="wf", name="wf",
        nodes=[WorkflowNode(id="n", kind="native", handler="echo", upstream=[])],
    )
    validate_workflow(rec, known_agent_ids=set(), known_handler_ids={"echo"})  # no raise


# ---------------------------------------------------------------------------
# Dispatch: run_native_node / registry
# ---------------------------------------------------------------------------


def test_echo_handler_is_registered():
    assert "echo" in NATIVE_HANDLERS


def test_get_native_handler_unknown_raises():
    with pytest.raises(ValueError, match="Unknown native handler"):
        get_native_handler("does-not-exist")


@pytest.mark.anyio
async def test_run_native_node_dispatches_to_echo(tmp_path):
    from hyperion.config import settings
    from hyperion.memory.context_store import context_get

    node = WorkflowNode(id="e1", kind="native", handler="echo", instruction="hello", upstream=[])
    ctx = NativeNodeCtx(task_id="t1", node=node, request="the goal")
    with patch.object(settings, "tasks_dir", tmp_path):
        res = await run_native_node(ctx)
        # The handler wrote to the blackboard (proves the write path).
        assert context_get("t1", "native_echo_e1") == "hello"
    assert res["handler"] == "echo"
    assert res["echo"] == "hello"
    assert res["node"] == "e1"
    assert res["request"] == "the goal"


@pytest.mark.anyio
async def test_run_native_node_unknown_handler_raises():
    node = WorkflowNode(id="x", kind="native", handler="missing", upstream=[])
    ctx = NativeNodeCtx(task_id="t1", node=node, request="r")
    with pytest.raises(ValueError, match="Unknown native handler"):
        await run_native_node(ctx)


@pytest.mark.anyio
async def test_register_custom_handler_and_dispatch(tmp_path):
    from hyperion.config import settings

    async def _double(ctx):
        return {"doubled": ctx.request * 2}

    register_native_handler("double_test", _double)
    try:
        node = WorkflowNode(id="d", kind="native", handler="double_test", upstream=[])
        ctx = NativeNodeCtx(task_id="t1", node=node, request="ab")
        with patch.object(settings, "tasks_dir", tmp_path):
            res = await run_native_node(ctx)
        assert res == {"doubled": "abab"}
    finally:
        NATIVE_HANDLERS.pop("double_test", None)


# ---------------------------------------------------------------------------
# Orchestration: a one-node native workflow runs end-to-end in the runner
# ---------------------------------------------------------------------------


def _native_workflow() -> WorkflowRecord:
    """A single native echo node — no agents, no subworkflows."""
    return WorkflowRecord(
        id="native-wf", name="native",
        nodes=[WorkflowNode(id="echo1", kind="native", handler="echo",
                            instruction="banked", upstream=[])],
    )


@contextlib.contextmanager
def _mock_native_run(tasks_dir):
    """Patch tasks_dir + the LLM-touching helpers so the run is fully offline.

    A native-only workflow never calls ``_run_stage``/``build_agent``, so only
    context discovery and the meta pipeline need mocking away.
    """
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tasks_dir), \
         patch.object(runner, "discover_context", MagicMock(return_value=None)), \
         patch("hyperion.crews.workflows.resolve_workflow",
               new=MagicMock(return_value=_native_workflow())), \
         patch("hyperion.server.meta_tasks.run_meta_tasks", new=AsyncMock()):
        yield


@pytest.mark.anyio
async def test_native_node_runs_end_to_end_in_runner(tmp_path):
    from hyperion.memory.context_store import context_get

    with _mock_native_run(tmp_path):
        result = await runner.run_task("nrun1", "prove the thing", workflow="native-wf")
        # Read the blackboard while tasks_dir is still patched to tmp_path.
        banked = context_get("nrun1", "native_echo_echo1")

    assert result["status"] == "done"
    # The native node fired and was recorded in routing like any other node.
    assert "echo1" in result["routing"]["selected_agents"]
    # The handler ran inside the DAG and wrote to the run's blackboard.
    assert banked == "banked"
