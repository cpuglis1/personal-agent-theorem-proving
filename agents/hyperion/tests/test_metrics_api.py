"""Phase 8 endpoint tests: /thresholds, /tasks (paginated), /metrics."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

import hyperion.server.api as api
from hyperion.server.api import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_db(tmp_path, rows):
    """Point the API at a fresh temp DB and insert (task_id, status, request, routing)."""
    db_path = tmp_path / "state.db"
    api._DB_PATH = db_path
    db = await api._get_db()
    for task_id, status, request, routing in rows:
        await db.execute(
            "INSERT INTO tasks (task_id, status, request, routing) VALUES (?,?,?,?)",
            (task_id, status, request, routing),
        )
    await db.commit()
    await db.close()
    return db_path


@pytest.mark.anyio
async def test_get_thresholds_shape():
    async with await _client() as client:
        resp = await client.get("/thresholds")
    assert resp.status_code == 200
    body = resp.json()
    assert "global" in body and "agents" in body
    assert "cap_wall_seconds" in body["global"]


@pytest.mark.anyio
async def test_put_thresholds_updates_global_cap(tmp_path, monkeypatch):
    from hyperion.config import settings

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    async with await _client() as client:
        resp = await client.put("/thresholds", json={"cap_wall_seconds": 123})
    assert resp.status_code == 200
    assert resp.json()["global"]["cap_wall_seconds"] == 123
    assert settings.cap_wall_seconds == 123
    saved = json.loads((tmp_path / "thresholds.json").read_text())
    assert saved["cap_wall_seconds"] == 123


@pytest.mark.anyio
async def test_put_thresholds_rejects_nonpositive(monkeypatch, tmp_path):
    from hyperion.config import settings

    monkeypatch.setattr(settings, "config_dir", tmp_path)
    async with await _client() as client:
        resp = await client.put("/thresholds", json={"cap_input_tokens": -5})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_tasks_pagination(tmp_path):
    await _seed_db(
        tmp_path,
        [
            ("aaa", "done", "first", None),
            ("bbb", "failed", "second", None),
            ("ccc", "running", "third", None),
        ],
    )
    async with await _client() as client:
        resp = await client.get("/tasks", params={"limit": 2, "offset": 0})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert {"task_id", "status", "request", "langfuse_url"} <= set(body["items"][0])


@pytest.mark.anyio
async def test_metrics_counts_activations_and_errors(tmp_path):
    routing = json.dumps({"selected_agents": ["researcher"], "skipped": [], "dag": {}})
    await _seed_db(
        tmp_path,
        [
            ("t1", "done", "ok run", routing),
            ("t2", "failed", "bad run", routing),
        ],
    )
    async with await _client() as client:
        resp = await client.get("/metrics")
    body = resp.json()
    assert body["tasks_total"] == 2
    by_id = {a["id"]: a for a in body["agents"]}
    assert by_id["researcher"]["activations"] == 2
    assert by_id["researcher"]["errors"] == 1
    assert by_id["researcher"]["error_rate"] == 0.5
