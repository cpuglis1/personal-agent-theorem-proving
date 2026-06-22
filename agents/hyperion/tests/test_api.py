"""
Integration tests for the Hyperion FastAPI service (``hyperion.server.api``).

This suite exercises the task lifecycle HTTP endpoints end-to-end using an
in-process ASGI transport (``httpx.ASGITransport``) instead of a live network
server. That keeps the tests fast and hermetic while still routing requests
through the real FastAPI app, routing, validation, and handler code.

Endpoints covered:
  - ``POST /tasks``                  : submit a new task (returns 202 + task_id)
  - ``GET  /tasks/{task_id}``        : poll task status (200 / 404)
  - ``POST /tasks/{task_id}/approve``: human-in-the-loop (HITL) approval
                                       (404 for unknown task, 409 when the task
                                       is not in an awaiting-approval state)

Key design decisions / non-obvious context:
  - Each test redirects ``settings.tasks_dir`` to pytest's ``tmp_path`` via
    ``patch.object`` so persisted task files are written to an isolated temp
    directory and never touch the real on-disk task store.
  - The background worker ``hyperion.server.api._run_and_update`` is replaced
    with an ``AsyncMock`` so submitting a task does NOT actually run the agent
    crew. As a result the task remains in its initial 'queued' state, which the
    409 test relies on (the task never reaches the paused/awaiting-approval
    state, so an approval is invalid).
  - ``hyperion.config.settings`` is imported lazily inside each test (rather
    than at module import time) so the patch is applied against the same
    singleton the app reads, and so import-time side effects are scoped.

Run: uv run pytest tests/test_api.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from hyperion.server.api import app


@pytest.fixture
def anyio_backend():
    """Pin the anyio/pytest-anyio backend to asyncio.

    The ``@pytest.mark.anyio`` tests below run on whatever backend this fixture
    yields. Returning only ``"asyncio"`` prevents anyio from also
    parametrizing the suite over the optional ``trio`` backend.

    Returns:
        str: The backend name ("asyncio") for anyio-marked async tests.
    """
    return "asyncio"


@pytest.mark.anyio
async def test_submit_and_poll(tmp_path):
    """Submitting a task returns 202 + a task_id, and that id is then pollable via GET (200).

    Args:
        tmp_path: pytest temp directory used as the isolated ``tasks_dir``.
    """
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        with patch("hyperion.server.api._run_and_update", new=AsyncMock()) as mock_run:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Submit task
                resp = await client.post("/tasks", json={"task": "test task"})
                assert resp.status_code == 202
                task_id = resp.json()["task_id"]
                assert task_id

                # Status endpoint
                resp2 = await client.get(f"/tasks/{task_id}")
                assert resp2.status_code == 200
                assert resp2.json()["task_id"] == task_id


@pytest.mark.anyio
async def test_submit_passes_eval_mode_and_lean_profile(tmp_path):
    """POST /tasks forwards benchmark discipline metadata to the runner."""
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        with patch("hyperion.server.api._run_and_update", new=AsyncMock()) as mock_run:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/tasks",
                    json={
                        "task": "prove",
                        "workflow": "lean-prove",
                        "eval_mode": "test",
                        "lean_profile": "mathlib",
                        "problem_id": "p1",
                        "split": "test",
                        "order_seed": 7,
                    },
                )
    assert resp.status_code == 202
    _, kwargs = mock_run.call_args
    assert kwargs["eval_mode"] == "test"
    assert kwargs["lean_profile"] == "mathlib"
    assert kwargs["problem_id"] == "p1"
    assert kwargs["split"] == "test"
    assert kwargs["order_seed"] == 7


@pytest.mark.anyio
async def test_startup_reconciles_stale_running_rows(tmp_path):
    """A restarted API cannot resume in-memory workers, so stale running rows go terminal."""
    from hyperion.config import settings
    from hyperion.server import api

    with patch.object(settings, "tasks_dir", tmp_path):
        api._PROGRESS.clear()
        db = await api._get_db()
        try:
            await db.execute(
                "INSERT INTO tasks (task_id, status, request) VALUES (?,?,?)",
                ("stale", "running", "prove"),
            )
            await db.execute(
                "INSERT INTO tasks (task_id, status, request) VALUES (?,?,?)",
                ("done", "done", "finished"),
            )
            await db.commit()
        finally:
            await db.close()

        await api._reconcile_interrupted_running_tasks()

        db = await api._get_db()
        try:
            async with db.execute(
                "SELECT task_id, status, error FROM tasks ORDER BY task_id"
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()

    by_id = {row[0]: {"status": row[1], "error": row[2]} for row in rows}
    assert by_id["stale"]["status"] == "failed"
    assert by_id["stale"]["error"].startswith("interrupted:")
    assert by_id["done"]["status"] == "done"
    assert "status=failed (interrupted:" in (tmp_path / "stale" / "progress.log").read_text(
        encoding="utf-8"
    )


@pytest.mark.anyio
async def test_missing_task_returns_404(tmp_path):
    """Polling an unknown task id returns 404 (no task file exists in tasks_dir).

    Args:
        tmp_path: pytest temp directory used as the isolated ``tasks_dir``.
    """
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/tasks/nonexistent")
            assert resp.status_code == 404


@pytest.mark.anyio
async def test_approve_on_non_awaiting_task_returns_409(tmp_path):
    """Approving a task that is not awaiting approval returns 409 Conflict.

    The task is submitted with ``hitl="plan"`` but ``_run_and_update`` is mocked,
    so it never advances to the paused/awaiting-approval state and an approval
    is therefore invalid.

    Args:
        tmp_path: pytest temp directory used as the isolated ``tasks_dir``.
    """
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        with patch("hyperion.server.api._run_and_update", new=AsyncMock()):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/tasks", json={"task": "t", "hitl": "plan"})
                task_id = resp.json()["task_id"]
                # _run_and_update is mocked, so the task stays 'queued' (never paused).
                resp2 = await client.post(f"/tasks/{task_id}/approve", json={"action": "approve"})
                assert resp2.status_code == 409


@pytest.mark.anyio
async def test_approve_missing_task_returns_404(tmp_path):
    """Approving an unknown task id returns 404 (the task does not exist).

    Args:
        tmp_path: pytest temp directory used as the isolated ``tasks_dir``.
    """
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/tasks/nope/approve", json={"action": "approve"})
            assert resp.status_code == 404
