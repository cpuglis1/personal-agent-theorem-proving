"""
Integration tests for the FastAPI service.

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
    return "asyncio"


@pytest.mark.anyio
async def test_submit_and_poll(tmp_path):
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
async def test_missing_task_returns_404(tmp_path):
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/tasks/nonexistent")
            assert resp.status_code == 404


@pytest.mark.anyio
async def test_approve_on_non_awaiting_task_returns_409(tmp_path):
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
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/tasks/nope/approve", json={"action": "approve"})
            assert resp.status_code == 404
