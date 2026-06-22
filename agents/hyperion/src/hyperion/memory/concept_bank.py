"""Concept bank — synthesized definitions plus verified bridge lemmas.

This is the long-term store for definition synthesis. A concept is banked after
``verify_concept`` proves every bridge soundness-clean and ``prove_through`` closes the
stuck theorem with the new vocabulary in scope. Writes mirror ``lemma_bank``: fail-soft
reads, loud structured writes.
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
            "times_won": int(concept.get("times_won") or 0),
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
        "times_won": int(payload.get("times_won") or 0),
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
