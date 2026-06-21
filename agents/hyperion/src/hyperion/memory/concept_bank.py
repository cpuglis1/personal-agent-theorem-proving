"""Concept bank — synthesized definitions plus verified bridge lemmas.

This is the long-term store for definition synthesis. A concept is born only after
``verify_concept`` proves every bridge soundness-clean and ``birth_ablation`` shows the
new vocabulary was causally necessary for the stuck theorem. Writes mirror
``lemma_bank``: fail-soft reads, loud structured writes.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional, TypedDict

from hyperion.config import settings
from hyperion.memory.lemma_bank import _embed, _ensure_collection, _get_clients, symbol_set

logger = logging.getLogger(__name__)

_ID_PREFIX = "concept:"
_SCORE_THRESHOLD = 0.25


class StoreResult(TypedDict):
    ok: bool
    id: Optional[str]
    error: Optional[str]


def _collection() -> str:
    return settings.qdrant_concepts_collection


def _definition_source(concept: dict[str, Any]) -> str:
    return str(((concept.get("definition") or {}).get("source") or "")).strip()


def _point_id(concept: dict[str, Any]) -> str:
    cid = str(concept.get("concept_id") or "").strip()
    key = cid or _definition_source(concept)
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{_ID_PREFIX}{key}"))


def _embedding_text(concept: dict[str, Any]) -> str:
    definition = concept.get("definition") or {}
    bridges = concept.get("bridges") or []
    parts = [
        str(definition.get("name") or ""),
        _definition_source(concept),
        "\n".join(str(b.get("lean_type") or b.get("statement") or "") for b in bridges),
    ]
    return "\n".join(p for p in parts if p).strip()


def store_concept(
    concept: dict[str, Any],
    *,
    source_goal: str = "",
    theorem_id: str = "",
    verified_at: int | None = None,
) -> StoreResult:
    """Embed and upsert an accepted synthesized concept.

    ``ok=False`` is returned and logged at ERROR when persistence fails; callers surface
    the loss in the native-node result instead of silently dropping a verified concept.
    """
    point_id = _point_id(concept)
    try:
        oai, qdrant = _get_clients()
        text = _embedding_text(concept)
        vector = _embed(oai, text or point_id)
        _ensure_collection(qdrant, len(vector), collection=_collection())

        from qdrant_client.models import PointStruct

        definition = concept.get("definition") or {}
        bridges = concept.get("bridges") or []
        payload = {
            "concept_id": concept.get("concept_id") or point_id,
            "definition": definition,
            "bridges": bridges,
            "origin": concept.get("origin") or "synthesized",
            "source_collection": _collection(),
            "source_goal": source_goal,
            "theorem_id": theorem_id,
            "born_theorem_index": concept.get("born_theorem_index"),
            "last_used_theorem_index": concept.get("last_used_theorem_index")
            or concept.get("born_theorem_index"),
            "times_won": int(concept.get("times_won") or 0),
            "necessity_hits": int(concept.get("necessity_hits") or 0),
            "provisional": bool(concept.get("provisional", True)),
            "birth_ablation": concept.get("birth_ablation") or {},
            "symbol_set": symbol_set(text),
            "last_used_at": verified_at if verified_at is not None else int(time.time()),
            "verified_at": verified_at if verified_at is not None else int(time.time()),
        }
        qdrant.upsert(
            collection_name=_collection(),
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        logger.info("Banked concept %s in %s", point_id, _collection())
        return {"ok": True, "id": point_id, "error": None}
    except Exception as exc:
        logger.error("Failed to bank concept %s: %s", point_id, exc)
        return {"ok": False, "id": point_id, "error": str(exc)}


def _payload_from_hit(hit: Any) -> dict[str, Any]:
    payload = hit.payload or {}
    return {
        "id": str(getattr(hit, "id", "") or ""),
        "concept_id": payload.get("concept_id", ""),
        "definition": payload.get("definition") or {},
        "bridges": payload.get("bridges") or [],
        "origin": payload.get("origin") or "synthesized",
        "source_collection": payload.get("source_collection") or _collection(),
        "source_goal": payload.get("source_goal", ""),
        "theorem_id": payload.get("theorem_id", ""),
        "born_theorem_index": payload.get("born_theorem_index"),
        "last_used_theorem_index": payload.get("last_used_theorem_index"),
        "times_won": int(payload.get("times_won") or 0),
        "necessity_hits": int(payload.get("necessity_hits") or 0),
        "provisional": bool(payload.get("provisional", True)),
        "birth_ablation": payload.get("birth_ablation") or {},
        "symbol_set": payload.get("symbol_set") or [],
        "verified_at": payload.get("verified_at"),
        "last_used_at": payload.get("last_used_at"),
        "score": round(float(getattr(hit, "score", 0.0) or 0.0), 3),
    }


def retrieve_concepts(goal: str, limit: int = 5) -> list[dict[str, Any]]:
    """Vector fallback for banked concepts; symbolic Lean search should rank first."""
    try:
        oai, qdrant = _get_clients()
        vector = _embed(oai, goal)
        response = qdrant.query_points(
            collection_name=_collection(),
            query=vector,
            limit=limit,
            score_threshold=_SCORE_THRESHOLD,
            with_payload=True,
        )
        return [_payload_from_hit(h) for h in response.points]
    except Exception as exc:
        logger.warning("Failed to retrieve concepts: %s", exc)
        return []


def mark_necessity_hit(
    concept: dict[str, Any],
    *,
    theorem_id: str,
    theorem_index: int | None = None,
) -> dict[str, Any]:
    """Best-effort stream-driver update for later-theorem necessity hits."""
    count = int(concept.get("necessity_hits") or 0) + 1
    provisional = count < settings.concept_promote_k
    point_id = concept.get("id") or _point_id(concept)
    try:
        _oai, qdrant = _get_clients()
        qdrant.set_payload(
            collection_name=_collection(),
            payload={
                "necessity_hits": count,
                "provisional": provisional,
                "last_necessity_theorem_id": theorem_id,
                "last_used_theorem_index": theorem_index,
                "last_used_at": int(time.time()),
            },
            points=[point_id],
        )
    except Exception as exc:
        logger.warning("Failed to update concept necessity counter: %s", exc)
    return {
        **concept,
        "necessity_hits": count,
        "provisional": provisional,
        "last_used_theorem_index": theorem_index,
    }


def prune_idle_concepts(
    concepts: list[dict[str, Any]],
    *,
    current_theorem_index: int,
) -> list[dict[str, Any]]:
    """Prune provisional concepts unused for ``concept_prune_idle_m`` later theorems.

    This is a stream-driver helper, not a per-run node. It expects payloads with
    ``last_used_theorem_index`` (or ``born_theorem_index``) and deletes only provisional
    concepts whose idle window has elapsed. Missing indices are kept conservatively.
    """
    pruned: list[dict[str, Any]] = []
    for concept in concepts:
        if not concept.get("provisional", True):
            continue
        last = concept.get("last_used_theorem_index")
        if last is None:
            last = concept.get("born_theorem_index")
        if last is None:
            continue
        try:
            idle = current_theorem_index - int(last)
        except (TypeError, ValueError):
            continue
        if idle < settings.concept_prune_idle_m:
            continue
        point_id = concept.get("id") or _point_id(concept)
        try:
            from qdrant_client.models import PointIdsList

            _oai, qdrant = _get_clients()
            qdrant.delete(
                collection_name=_collection(),
                points_selector=PointIdsList(points=[point_id]),
            )
            pruned.append({**concept, "pruned": True, "idle_theorems": idle})
        except Exception as exc:
            logger.warning("Failed to prune idle concept %s: %s", point_id, exc)
    return pruned
