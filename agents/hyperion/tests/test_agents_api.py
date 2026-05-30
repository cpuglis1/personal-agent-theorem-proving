"""Agent CRUD + options API tests (PLAN_UNIFIED.md Phase 5)."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from hyperion.agents.registry import AgentRecord, Trigger, save_agent
from hyperion.server.api import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _rec(id_, stage, active=True, order=1, tools=None, upstream=None):
    trig = Trigger(type="upstream", upstream=upstream) if upstream else Trigger(type="always")
    return AgentRecord(
        id=id_, name=id_, role=id_, goal="g", backstory="b",
        stage=stage, active=active, order=order, tools=tools or [], trigger=trig,
    )


@contextlib.contextmanager
def _env(tmp_path):
    """Patch config_dir to an isolated store seeded with a minimal valid set, and
    stub the LiteLLM model lookup so model validation never hits the network."""
    from hyperion.config import settings

    with patch.object(settings, "config_dir", tmp_path), \
         patch("hyperion.server.api._litellm_model_ids", new=AsyncMock(return_value=[])):
        save_agent(_rec("planner", "plan"))
        save_agent(_rec("researcher", "work", tools=["web_search"]))
        save_agent(_rec("synthesizer", "synthesize"))
        yield


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_list_and_get_agents(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            resp = await c.get("/agents")
            assert resp.status_code == 200
            assert {a["id"] for a in resp.json()} == {"planner", "researcher", "synthesizer"}
            assert (await c.get("/agents/planner")).json()["stage"] == "plan"
            assert (await c.get("/agents/ghost")).status_code == 404


@pytest.mark.anyio
async def test_create_agent_participates(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            body = _rec("developer", "work", tools=["web_search"], order=2).model_dump()
            resp = await c.post("/agents", json=body)
            assert resp.status_code == 201
            ids = {a["id"] for a in (await c.get("/agents")).json()}
            assert "developer" in ids


@pytest.mark.anyio
async def test_create_duplicate_id_conflicts(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            resp = await c.post("/agents", json=_rec("planner", "plan").model_dump())
            assert resp.status_code == 409


@pytest.mark.anyio
async def test_create_unknown_tool_rejected(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            body = _rec("badtool", "work", tools=["does_not_exist"]).model_dump()
            resp = await c.post("/agents", json=body)
            assert resp.status_code == 422
            assert "unknown tool" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_delete_last_synthesize_rejected(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            resp = await c.delete("/agents/synthesizer")
            assert resp.status_code == 422
            assert "synthesize" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_delete_optional_agent_ok(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            await c.post("/agents", json=_rec("developer", "work", order=2).model_dump())
            resp = await c.delete("/agents/developer")
            assert resp.status_code == 200
            assert resp.json()["deleted"] == "developer"


@pytest.mark.anyio
async def test_cycle_rejected(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            # a depends on b
            await c.post("/agents", json=_rec("a", "work", order=2, upstream=["b"]).model_dump())
            # b depends on a → introduces a cycle → rejected
            resp = await c.post("/agents", json=_rec("b", "work", order=3, upstream=["a"]).model_dump())
            assert resp.status_code == 422
            assert "cycle" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_duplicate_agent(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            resp = await c.post("/agents/researcher/duplicate")
            assert resp.status_code == 201
            assert resp.json()["id"] == "researcher-copy"


@pytest.mark.anyio
async def test_tools_and_models_endpoints(tmp_path):
    with _env(tmp_path):
        async with _client() as c:
            tools = (await c.get("/tools")).json()
            assert "web_search" in {t["name"] for t in tools}
            models = (await c.get("/models")).json()
            assert "smart" in models["aliases"]
            assert "planner" in models["current"]


@pytest.mark.anyio
async def test_put_config_reassigns_model(tmp_path):
    from hyperion.config import settings

    with _env(tmp_path):
        async with _client() as c:
            resp = await c.put("/config", json={"model_planner": "fast"})
            assert resp.status_code == 200
            assert settings.model_planner == "fast"
            # persisted for next boot
            assert (tmp_path / "models.json").exists()


@pytest.mark.anyio
async def test_put_config_rejects_unknown_model(tmp_path):
    # With a populated model list, an unknown id is rejected.
    from hyperion.config import settings

    with patch.object(settings, "config_dir", tmp_path), \
         patch("hyperion.server.api._litellm_model_ids", new=AsyncMock(return_value=["gpt-4o"])):
        save_agent(_rec("planner", "plan"))
        save_agent(_rec("synthesizer", "synthesize"))
        async with _client() as c:
            resp = await c.put("/config", json={"model_worker": "nope-model"})
            assert resp.status_code == 422
