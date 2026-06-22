"""The prover stage trace is exposed on the live HTTP + MCP surface (Post-work observability).

The native prover stages (retrieve/verify/prove_through/bank) are not LLM ``trace_events``,
so they don't show up in the existing Trace Flow UI. These tests prove the tracer is wired in:

  - ``GET /tasks/{id}/trace`` gains a ``prover`` field (the per-stage, per-sub-goal trace) for
    prover runs, and ``null`` for ordinary tasks.
  - the MCP ``hyperion_trace`` tool renders the same trace as text (and reports cleanly for a
    non-prover task).

Both read the durable blackboard via ``eval.trace.trace_task`` — no live toolchain needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from hyperion.config import settings
from hyperion.memory.context_store import context_put
from hyperion.server import mcp
from hyperion.server.api import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


_PLAN = """---
task_id: {tid}
task_type: code
scaffold: |
  theorem t : P := by
    have h1 : P := sorry
    exact h1
options:
  - id: a
    summary: demo
    subtasks:
      - id: h1
        description: prove P
        lean_type: "P"
---
"""


def _seed_prover_blackboard(task_id: str, tasks_dir) -> None:
    """Write a plan + a representative single-sub-goal prover blackboard for ``task_id``."""
    (tasks_dir / task_id).mkdir(parents=True, exist_ok=True)
    (tasks_dir / task_id / "plan.md").write_text(_PLAN.format(tid=task_id), encoding="utf-8")
    context_put(task_id, "candidate_a:h1",
                {"origin": "retrieve", "path": "A", "lean_type": "P", "proof_term": "pa"})
    context_put(task_id, "verified_a:h1", {"path": "A"})
    context_put(task_id, "verify_decision:h1",
                {"mode": "deploy", "a_attempts": 1, "repair_iters": 0})
    context_put(task_id, "discharged:h1",
                {"origin": "retrieve", "path": "A", "proof_term": "pa", "lean_type": "P"})


# ---------------------------------------------------------------------------
# HTTP — GET /tasks/{id}/trace gains the `prover` field
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trace_endpoint_includes_prover_stages(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path), \
         patch("hyperion.server.api._run_and_update", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            task_id = (await client.post("/tasks", json={"task": "prove P"})).json()["task_id"]
            _seed_prover_blackboard(task_id, tmp_path)

            body = (await client.get(f"/tasks/{task_id}/trace")).json()

    assert body["prover"] is not None
    sub = body["prover"]["subgoals"]["h1"]
    assert sub["discharged"]["path"] == "A"
    # The generic trace fields are still present (additive, didn't break the UI contract).
    assert "events" in body and "routing" in body


@pytest.mark.anyio
async def test_trace_endpoint_prover_null_for_ordinary_task(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path), \
         patch("hyperion.server.api._run_and_update", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            task_id = (await client.post("/tasks", json={"task": "ordinary task"})).json()["task_id"]
            body = (await client.get(f"/tasks/{task_id}/trace")).json()

    assert body["prover"] is None


# ---------------------------------------------------------------------------
# MCP — hyperion_trace tool renders the stage trace
# ---------------------------------------------------------------------------


async def _insert_task(task_id: str, status: str, request: str) -> None:
    await mcp._ensure_db()
    async with aiosqlite.connect(mcp._db_path()) as db:
        await db.execute(
            "INSERT INTO tasks (task_id, status, request) VALUES (?, ?, ?)",
            (task_id, status, request),
        )
        await db.commit()


@pytest.mark.anyio
async def test_mcp_hyperion_trace_renders_stages(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        await _insert_task("ptask", "done", "prove P")
        _seed_prover_blackboard("ptask", tmp_path)

        out = await mcp.call_tool("hyperion_trace", {"task_id": "ptask"})

    text = out[0].text
    for label in ("sub-goal h1", "retrieve", "verify", "prove through", "result.lean"):
        assert label in text


@pytest.mark.anyio
async def test_mcp_hyperion_trace_non_prover_task_is_friendly(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        await _insert_task("plain", "done", "ordinary task")  # no prover blackboard
        out = await mcp.call_tool("hyperion_trace", {"task_id": "plain"})

    assert "no prover stage trace" in out[0].text.lower()


@pytest.mark.anyio
async def test_mcp_hyperion_trace_unknown_task(tmp_path):
    with patch.object(settings, "tasks_dir", tmp_path):
        await mcp._ensure_db()
        out = await mcp.call_tool("hyperion_trace", {"task_id": "ghost"})
    assert "not found" in out[0].text.lower()
