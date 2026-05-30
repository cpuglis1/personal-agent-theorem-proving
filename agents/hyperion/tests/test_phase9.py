"""Phase 9 tests: agent card, callback SSRF guard, config export/import round-trip."""

from __future__ import annotations

import io
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

import hyperion.server.api as api
from hyperion.server import webhooks
from hyperion.server.api import app
from hyperion.server.webhooks import UnsafeCallbackURL, validate_callback_url


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- SSRF guard -------------------------------------------------------------


def test_callback_guard_allows_loopback():
    validate_callback_url("http://127.0.0.1:9000/hook")  # no raise


def test_callback_guard_rejects_public_host(monkeypatch):
    # Force a public-looking resolution regardless of the test host's DNS.
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )
    with pytest.raises(UnsafeCallbackURL):
        validate_callback_url("http://example.com/hook")


def test_callback_guard_rejects_non_http_scheme():
    with pytest.raises(UnsafeCallbackURL):
        validate_callback_url("file:///etc/passwd")


def test_callback_guard_off_skips_check(monkeypatch):
    from hyperion.config import settings

    monkeypatch.setattr(settings, "hyperion_callback_ssrf_guard", "off")
    validate_callback_url("http://example.com/anything")  # no raise


# --- agent card -------------------------------------------------------------


@pytest.mark.anyio
async def test_agent_card_descriptor():
    async with await _client() as client:
        resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["schema_version"] == 1
    assert card["name"] == "Hyperion"
    assert "submit" in card["endpoints"]
    assert isinstance(card["skills"], list)


# --- submit validation ------------------------------------------------------


@pytest.mark.anyio
async def test_submit_rejects_bad_schema_version(tmp_path):
    api._DB_PATH = tmp_path / "state.db"
    await (await api._get_db()).close()
    async with await _client() as client:
        resp = await client.post("/tasks", json={"task": "x", "schema_version": 99})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_submit_rejects_unsafe_callback(tmp_path, monkeypatch):
    api._DB_PATH = tmp_path / "state.db"
    await (await api._get_db()).close()
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 80))],
    )
    async with await _client() as client:
        resp = await client.post(
            "/tasks", json={"task": "x", "callback_url": "http://evil.example/hook"}
        )
    assert resp.status_code == 422


# --- config export / import round-trip --------------------------------------


@pytest.mark.anyio
async def test_config_export_import_roundtrip(tmp_path, monkeypatch):
    from hyperion.config import settings

    monkeypatch.setattr(settings, "config_dir", tmp_path)

    # Seed a minimal valid store: one plan, one synthesize agent.
    from hyperion.agents.registry import AgentRecord, save_agent

    save_agent(AgentRecord(id="planner", name="Planner", stage="plan",
                           role="r", goal="g", backstory="b"))
    save_agent(AgentRecord(id="synth", name="Synth", stage="synthesize",
                           role="r", goal="g", backstory="b"))

    async with await _client() as client:
        export = await client.get("/config/export")
        assert export.status_code == 200
        assert export.headers["content-type"] == "application/zip"

        names = zipfile.ZipFile(io.BytesIO(export.content)).namelist()
        assert "agents/planner.json" in names

        # Wipe the store, then import the exported zip back.
        for p in tmp_path.glob("agents/*.json"):
            p.unlink()
        resp = await client.post(
            "/config/import",
            files={"file": ("cfg.zip", export.content, "application/zip")},
        )
    assert resp.status_code == 200
    assert set(resp.json()["imported"]) == {"planner", "synth"}
    assert (tmp_path / "agents" / "planner.json").exists()


@pytest.mark.anyio
async def test_config_import_rejects_bad_zip():
    async with await _client() as client:
        resp = await client.post(
            "/config/import",
            files={"file": ("x.zip", b"not a zip", "application/zip")},
        )
    assert resp.status_code == 422
