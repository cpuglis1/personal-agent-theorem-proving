"""
litellm_admin.py — reconcile operator-defined model aliases into the LiteLLM proxy.

Role in the system:
  Hyperion lets operators define new model *aliases* (multi-provider groups with an
  ordered fallback chain) from the UI; see ``models_registry``. The registry is
  Hyperion's source of truth, but an alias only actually *routes* once it exists in the
  LiteLLM proxy. This module is the write-through: it registers/removes the proxy
  deployments backing a user-defined alias via LiteLLM's admin API
  (``/model/new`` / ``/model/delete`` / ``/model/info``).

  Requires ``general_settings.store_model_in_db: true`` in ``litellm_config.yaml`` so the
  admin API persists into the existing Postgres ``litellm`` DB. The YAML ``model_list``
  remains the bootstrap base; UI-defined aliases are layered on top as DB models.

Key design decisions:
  - **Built-in aliases (smart/worker/cheap/fast) are never reconciled.** Their routing is
    owned by ``litellm_config.yaml``; editing their chain in the registry changes the
    displayed/Hyperion-side chain only. New (user-defined) aliases get real routing.
  - A new alias's deployments are derived by **looking up each concrete model's existing
    deployment** in ``/model/info`` (to reuse the exact provider-prefixed upstream id —
    e.g. ``claude-haiku-4-5`` actually maps to ``anthropic/claude-haiku-4-5-20251001``)
    and supplying the API key as an ``os.environ/<KEY>`` reference resolved at call time,
    rather than copying the proxy's redacted key.
  - Reconcile is **idempotent**: it adds only missing deployments and deletes only ones
    that no longer belong, so repeated calls converge.

Admin endpoints live at the proxy *root* (``http://litellm:4000``), not under ``/v1``;
``_admin_base`` strips a trailing ``/v1`` from ``settings.litellm_base_url``.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from hyperion.config import settings
from hyperion import models_registry

logger = logging.getLogger(__name__)

# LiteLLM runs with multiple uvicorn workers (``--num_workers``), each with its own
# in-memory copy of the DB-backed model list that syncs on an interval. So a
# deployment added via /model/new is briefly visible to only some workers, and a
# /model/info read (load-balanced across workers) can miss a just-created deployment.
# The delete path retries the read this many times so cleanup reliably finds the
# deployment's id even right after it was created (otherwise it orphans in the DB).
_DELETE_LOOKUP_ATTEMPTS = 8
_DELETE_LOOKUP_DELAY = 0.5  # seconds between retries while the id is not yet visible

# Provider prefix (from a deployment's ``litellm_params.model``) -> the env var holding
# that provider's key. Matches the ``os.environ/<NAME>`` references in litellm_config.yaml.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "vertex_ai": "GEMINI_API_KEY",
}

_TIMEOUT = 10.0


def _admin_base() -> str:
    """Proxy root URL for admin endpoints (``litellm_base_url`` minus a trailing ``/v1``)."""
    base = settings.litellm_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _headers() -> dict[str, str]:
    """Bearer auth header using the master key (admin endpoints require it)."""
    key = settings.litellm_master_key or settings.llm_api_key
    return {"Authorization": f"Bearer {key}"}


def _key_ref_for(upstream_model: str) -> str | None:
    """Return an ``os.environ/<ENV>`` api_key reference for a provider-prefixed model.

    Args:
        upstream_model: e.g. ``"anthropic/claude-haiku-4-5-20251001"``.

    Returns:
        ``"os.environ/ANTHROPIC_API_KEY"`` (etc.), or ``None`` if the provider prefix is
        unrecognized (the deployment is still registered, just without an explicit key).
    """
    provider = upstream_model.split("/", 1)[0] if "/" in upstream_model else ""
    env = _PROVIDER_KEY_ENV.get(provider)
    return f"os.environ/{env}" if env else None


async def _model_info(client: httpx.AsyncClient) -> list[dict]:
    """Return the proxy's current deployment list (``GET /model/info`` → ``data``)."""
    resp = await client.get(f"{_admin_base()}/model/info", headers=_headers())
    resp.raise_for_status()
    return resp.json().get("data", [])


def _upstream_by_model_name(info: list[dict]) -> dict[str, str]:
    """Map each ``model_name`` to its first deployment's upstream ``litellm_params.model``.

    Used to resolve a bare concrete model id (e.g. ``gpt-4o``) to the provider-prefixed
    upstream id LiteLLM actually calls (e.g. ``openai/gpt-4o``).
    """
    out: dict[str, str] = {}
    for d in info:
        name = d.get("model_name")
        upstream = (d.get("litellm_params") or {}).get("model")
        if name and upstream and name not in out:
            out[name] = upstream
    return out


def _deployments_for(info: list[dict], alias: str) -> list[dict]:
    """Return deployments registered under ``model_name == alias`` (id + upstream)."""
    out = []
    for d in info:
        if d.get("model_name") == alias:
            out.append(
                {
                    "id": (d.get("model_info") or {}).get("id"),
                    "upstream": (d.get("litellm_params") or {}).get("model"),
                }
            )
    return out


async def _collect_deployment_ids(client: httpx.AsyncClient, alias: str) -> set[str]:
    """Find all deployment ids registered under ``alias``, tolerating worker-cache lag.

    Reads ``/model/info`` up to ``_DELETE_LOOKUP_ATTEMPTS`` times. Because LiteLLM's
    multiple workers each cache the model list independently, a single read right after
    a create can miss the deployment; retrying until a synced worker answers makes
    cleanup reliable. The deployment id is a stable DB-row id (identical across workers),
    so the first non-empty read is authoritative and we stop early.

    Args:
        client: An open httpx client.
        alias: The alias (``model_name``) whose deployment ids to collect.

    Returns:
        The set of deployment ids found (empty if the alias genuinely has none).
    """
    ids: set[str] = set()
    for attempt in range(_DELETE_LOOKUP_ATTEMPTS):
        info = await _model_info(client)
        for dep in _deployments_for(info, alias):
            if dep["id"]:
                ids.add(dep["id"])
        if ids:
            break  # found it on a synced worker — ids are stable, no need to keep reading
        if attempt < _DELETE_LOOKUP_ATTEMPTS - 1:
            await asyncio.sleep(_DELETE_LOOKUP_DELAY)
    return ids


async def _add_deployment(client: httpx.AsyncClient, alias: str, upstream: str) -> None:
    """Register one deployment (``upstream``) under ``model_name == alias``."""
    params: dict[str, str] = {"model": upstream}
    key_ref = _key_ref_for(upstream)
    if key_ref:
        params["api_key"] = key_ref
    resp = await client.post(
        f"{_admin_base()}/model/new",
        headers=_headers(),
        json={"model_name": alias, "litellm_params": params},
    )
    resp.raise_for_status()


async def _delete_deployment(client: httpx.AsyncClient, model_id: str) -> None:
    """Delete one deployment by its ``model_info.id``."""
    resp = await client.post(
        f"{_admin_base()}/model/delete",
        headers=_headers(),
        json={"id": model_id},
    )
    resp.raise_for_status()


async def reconcile_alias(name: str, models: list[str] | None) -> dict:
    """Make the proxy's deployments for alias ``name`` match the desired model chain.

    Args:
        name: Alias name (e.g. ``"vision"``).
        models: Desired ordered concrete model ids, or ``None`` to remove the alias's
            deployments entirely (used on delete).

    Returns:
        A status dict: ``{"status": "applied"|"partial"|"deleted"|"builtin"|"error",
        "detail": str?}``. Never raises — callers treat the registry edit as the source
        of truth and surface this status to the UI.
    """
    if name in models_registry.BUILTIN_ALIASES:
        return {"status": "builtin", "detail": "routing defined in litellm_config.yaml"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Delete path: remove every deployment under this alias. Use the retrying
            # lookup so a delete shortly after a create still finds the id (workers lag).
            if models is None:
                for dep_id in await _collect_deployment_ids(client, name):
                    await _delete_deployment(client, dep_id)
                return {"status": "deleted"}

            info = await _model_info(client)
            existing = _deployments_for(info, name)
            upstream_map = _upstream_by_model_name(info)
            desired_upstreams: list[str] = []
            missing_models: list[str] = []
            for m in models:
                upstream = upstream_map.get(m)
                if upstream:
                    desired_upstreams.append(upstream)
                else:
                    missing_models.append(m)

            existing_upstreams = {d["upstream"] for d in existing}

            # Add desired deployments that aren't present yet.
            for upstream in desired_upstreams:
                if upstream not in existing_upstreams:
                    await _add_deployment(client, name, upstream)

            # Remove deployments that are no longer desired.
            for dep in existing:
                if dep["upstream"] not in desired_upstreams and dep["id"]:
                    await _delete_deployment(client, dep["id"])

            if missing_models:
                return {
                    "status": "partial",
                    "detail": f"no proxy deployment found for: {', '.join(missing_models)}",
                }
            return {"status": "applied"}
    except Exception as exc:
        logger.warning("reconcile_alias(%r) failed: %s", name, exc)
        return {"status": "error", "detail": str(exc)}


async def alias_routing_status(aliases: dict[str, list[str]]) -> dict[str, dict]:
    """Report each alias's live routing status against the proxy (read-only).

    Args:
        aliases: ``name -> desired concrete model ids`` (typically ``models_registry.aliases()``).

    Returns:
        ``name -> {"status": "builtin"|"applied"|"partial"|"pending"|"unknown"}``.
        ``builtin`` aliases route via YAML; ``applied`` means every desired model has a
        deployment; ``partial`` some; ``pending`` none registered yet; ``unknown`` if the
        proxy/admin API is unreachable.
    """
    out: dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            info = await _model_info(client)
    except Exception as exc:
        logger.warning("alias_routing_status: could not reach proxy admin API: %s", exc)
        for name in aliases:
            out[name] = {"status": "builtin" if name in models_registry.BUILTIN_ALIASES else "unknown"}
        return out

    upstream_map = _upstream_by_model_name(info)
    for name, models in aliases.items():
        if name in models_registry.BUILTIN_ALIASES:
            out[name] = {"status": "builtin"}
            continue
        existing_upstreams = {d["upstream"] for d in _deployments_for(info, name)}
        desired = [upstream_map.get(m) for m in models]
        present = [u for u in desired if u and u in existing_upstreams]
        if present and len(present) == len([u for u in desired if u]):
            out[name] = {"status": "applied"}
        elif present:
            out[name] = {"status": "partial"}
        else:
            out[name] = {"status": "pending"}
    return out
