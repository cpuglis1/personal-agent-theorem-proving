"""
Episodic memory — stores completed task summaries in Qdrant ``hyperion_memory``.

Role in the system
------------------
This module is Hyperion's long-term "what have I done before" memory. After each
completed run, the orchestrator persists a single semantic record summarizing the
task so that future runs can learn from past work. The Planner agent can call
:func:`recall_similar_tasks` to look up similar prior tasks before producing a new
plan, giving the system a lightweight form of experience reuse.

After each completed run, one record is stored:
  {task_id, original_request, final_summary, models_used, cost, duration, success}

The Planner can call recall_similar_tasks() to look up past work before planning.

Key design decisions / non-obvious context
-------------------------------------------
- Storage backend is Qdrant (collection ``hyperion_memory``), with embeddings
  produced via the OpenAI-compatible LiteLLM proxy (``text-embedding-3-small``).
  Per project convention, all LLM/embedding traffic is routed through the proxy
  rather than calling provider APIs directly.
- Heavy clients (``openai``, ``qdrant_client``) are imported lazily inside helper
  functions so that importing this module is cheap and does not require those
  packages at import time.
- Both public functions are intentionally fault-tolerant: any failure (network,
  missing collection, embedding error) is logged and swallowed. Memory is a
  best-effort enhancement — it must never break or block a run.
- Point IDs are deterministic UUID5 values derived from ``task_id`` so that
  re-storing the same task upserts (replaces) the existing record rather than
  creating a duplicate.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from hyperion.config import settings

logger = logging.getLogger(__name__)

_COLLECTION = "hyperion_memory"


def _get_clients():
    from openai import OpenAI
    from qdrant_client import QdrantClient

    oai = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
    qdrant = QdrantClient(url=settings.qdrant_url)
    return oai, qdrant


def _embed(oai, text: str) -> list[float]:
    return oai.embeddings.create(model="text-embedding-3-small", input=text).data[0].embedding


def store_episode(
    task_id: str,
    original_request: str,
    final_summary: str,
    success: bool,
    duration_seconds: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        oai, qdrant = _get_clients()
        vector = _embed(oai, f"{original_request}\n\n{final_summary}")

        from qdrant_client.models import PointStruct

        # Deterministic UUID per task_id (UUID5 with DNS namespace; hash() is
        # non-deterministic across Python processes due to PYTHONHASHSEED).
        point = PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"hyperion-task-{task_id}")),
            vector=vector,
            payload={
                "task_id": task_id,
                "original_request": original_request,
                "final_summary": final_summary[:2000],
                "success": success,
                "duration_seconds": duration_seconds,
                "stored_at": int(time.time()),
                **(metadata or {}),
            },
        )
        qdrant.upsert(collection_name=_COLLECTION, points=[point])
        logger.info("Stored episode for task %s in %s", task_id, _COLLECTION)
    except Exception as exc:
        logger.warning("Failed to store episode: %s", exc)


def recall_similar_tasks(query: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        oai, qdrant = _get_clients()
        vector = _embed(oai, query)
        response = qdrant.query_points(
            collection_name=_COLLECTION,
            query=vector,
            limit=limit,
            score_threshold=0.3,
            with_payload=True,
        )
        return [
            {
                "task_id": h.payload.get("task_id"),
                "request": h.payload.get("original_request", ""),
                "summary": h.payload.get("final_summary", ""),
                "success": h.payload.get("success", False),
                "score": round(h.score, 3),
            }
            for h in response.points
        ]
    except Exception as exc:
        logger.warning("Failed to recall episodes: %s", exc)
        return []
