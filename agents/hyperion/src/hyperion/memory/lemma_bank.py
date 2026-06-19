"""
Lemma bank — stores verified Lean lemmas in Qdrant ``lemma_bank``.

Role in the system
------------------
This is the prover's long-term "what have I already proved" memory and the engine of
the snowball: every verified lemma is embedded and persisted so future sub-goals can
*retrieve* it (Path A) instead of re-synthesizing it (Path B). It is a re-skin of
:mod:`hyperion.memory.episodic` (build plan Phase 2 / baseline §3 "tune") — same
Qdrant client, lazy imports, deterministic UUID5 upsert — with three changes:

1. **Embedding text** is the *lemma statement / goal type*, not ``request + summary``.
2. **Payload schema** is ``{statement, proof_term, generality_score, source_goal,
   verified_at, verification_mode}`` (replaces the task-episode payload).
3. **Dedup identity** is a UUID5 over the *normalized statement* (not a ``task_id``),
   so re-deriving the same lemma upserts in place instead of duplicating.

Load-bearing write path (baseline risk #4)
------------------------------------------
Episodic memory swallows *all* errors because "memory is a nice-to-have." For the
prover a lost write loses a *verified* lemma and stalls the snowball — it is
load-bearing for the thesis. So the posture here is split:

  - **Reads stay fail-soft:** :func:`retrieve_lemmas` logs a warning and returns
    ``[]`` on any failure (a degraded retrieval should never break a run).
  - **Writes are loud:** :func:`store_lemma` logs at ``ERROR`` and returns a
    structured :class:`StoreResult` (``ok=False`` + ``error``) so the caller (the
    Phase 5 ``bank`` handler) can surface the loss to the run result. It still does
    not *raise* — a banking failure must not kill an otherwise-good proof run — but
    the loss is now **observable**, never silently swallowed.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Optional, TypedDict

from hyperion.config import settings

logger = logging.getLogger(__name__)

# Namespace prefix for the deterministic UUID5 point id. Distinct from episodic's
# ``hyperion-task-`` prefix so the two stores can never collide on an id.
_ID_PREFIX = "lemma:"

# Retrieval over-fetch floor mirrors episodic's recall threshold; coarse vector gate
# before any (Phase 3) applicability filtering.
_SCORE_THRESHOLD = 0.3


class StoreResult(TypedDict):
    """The outcome of a :func:`store_lemma` write — loud by design (risk #4).

    Attributes:
        ok: True iff the lemma was persisted. ``False`` ⇒ a *verified* lemma was
            NOT banked; the caller must surface this (the snowball stalls otherwise).
        id: The deterministic point id when known (set even on a failed upsert, so a
            retry can target the same point), else None.
        error: A human-readable failure reason when ``ok`` is False, else None.
    """

    ok: bool
    id: Optional[str]
    error: Optional[str]


def _collection() -> str:
    """Qdrant collection backing the lemma bank.

    Config-driven (``settings.qdrant_lemma_collection``) rather than a hardcoded
    literal so the bank can be re-namespaced per deployment/domain. Read on each call
    so a test patching ``settings`` takes effect without reimport.
    """
    return settings.qdrant_lemma_collection


def _get_clients():
    from openai import OpenAI
    from qdrant_client import QdrantClient

    oai = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
    qdrant = QdrantClient(url=settings.qdrant_url)
    return oai, qdrant


def _embed(oai, text: str) -> list[float]:
    return oai.embeddings.create(model="text-embedding-3-small", input=text).data[0].embedding


def _ensure_collection(qdrant, dims: int) -> None:
    """Idempotently provision the lemma-bank collection before a write.

    Seed-Prover-style self-healing: the bank creates its backing Qdrant collection
    on first write (cosine, ``dims``-wide — derived from the live embedding so it can
    never drift from the model) instead of relying on a one-shot bootstrap script. A
    wiped/fresh Qdrant volume therefore self-provisions on the next banked lemma rather
    than silently 404-ing every ``store_lemma`` (the prior failure mode: the collection
    never existed, so the snowball could never start). No-op when it already exists.
    """
    from qdrant_client.models import Distance, VectorParams

    collection = _collection()
    if qdrant.collection_exists(collection):
        return
    qdrant.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=dims, distance=Distance.COSINE),
    )
    logger.info("Created lemma-bank collection %s (dims=%d, cosine)", collection, dims)


def _normalize(statement: str) -> str:
    """Normalize a lemma statement for stable dedup identity.

    Scoped to **whitespace** for this phase: collapse all runs of whitespace to a
    single space and strip the ends, so ``"a   :=\n  b"`` and ``"a := b"`` hash to the
    same point id. The result feeds the UUID5, so two textually-identical-modulo-
    whitespace lemmas upsert to one point.

    TODO (out of scope for Phase 2): true alpha-equivalence (bound-variable renaming)
    needs Lean binder parsing; a whitespace pass is the deterministic, dependency-free
    floor. Semantic near-duplicate collapse is a Phase 3/5 concern (applicability gate
    + score_threshold), not a normalization concern here.
    """
    return re.sub(r"\s+", " ", statement).strip()


def _point_id(statement: str) -> str:
    """Deterministic UUID5 point id for ``statement`` (UUID5 over the normalized form).

    Re-deriving the same lemma yields the same id, so the upsert replaces rather than
    duplicates. (UUID5, not ``hash()`` — ``hash`` is non-deterministic across processes
    under ``PYTHONHASHSEED``.)
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{_ID_PREFIX}{_normalize(statement)}"))


def store_lemma(
    statement: str,
    proof_term: str,
    *,
    generality_score: float = 0.0,
    source_goal: str = "",
    verification_mode: str = "full",
    verified_at: int | None = None,
    lean_type: str | None = None,
) -> StoreResult:
    """Embed and upsert a verified lemma into the bank.

    The write path is **loud** (risk #4): a failure logs at ``ERROR`` and returns
    ``StoreResult(ok=False, ...)`` so the caller can surface the lost lemma. It never
    raises — a banking failure must not kill an otherwise-good proof run.

    Args:
        statement: The lemma / goal type. Embedded for retrieval and normalized for
            the dedup identity.
        proof_term: The verified proof closing ``statement``.
        generality_score: Reusability score (Phase 5 compare); stored now.
        source_goal: The originating sub-goal this lemma was derived for.
        verification_mode: How it was verified (``"full"`` / ``"skeleton"``).
        verified_at: Epoch seconds of verification; defaults to now when unset.
        lean_type: The *bare* Lean proposition this lemma proves (the sub-goal's
            ``lean_type`` from the plan contract), stored as a first-class payload
            field so the Phase-3 applicability probe can use it directly instead of
            re-deriving it from ``statement`` via the ``_lemma_type`` heuristic
            (build plan Phase 4 decision b). Optional/defaulted: ``None`` omits the
            field, keeping the payload backward-compatible with pre-Phase-4 writes.

    Returns:
        A :class:`StoreResult`. ``ok=True`` ⇒ persisted (one point, deterministic id);
        ``ok=False`` ⇒ NOT persisted and the reason is in ``error`` (also logged at
        ERROR).
    """
    point_id = _point_id(statement)
    try:
        oai, qdrant = _get_clients()
        vector = _embed(oai, statement)
        _ensure_collection(qdrant, len(vector))

        from qdrant_client.models import PointStruct

        payload: dict[str, Any] = {
            "statement": statement,
            "proof_term": proof_term,
            "generality_score": generality_score,
            "source_goal": source_goal,
            "verified_at": verified_at if verified_at is not None else int(time.time()),
            "verification_mode": verification_mode,
        }
        if lean_type is not None:
            payload["lean_type"] = lean_type
        point = PointStruct(
            id=point_id,
            vector=vector,
            payload=payload,
        )
        collection = _collection()
        qdrant.upsert(collection_name=collection, points=[point])
        logger.info("Banked lemma %s in %s", point_id, collection)
        return {"ok": True, "id": point_id, "error": None}
    except Exception as exc:
        # LOUD: a failed write loses a verified lemma and stalls the snowball. Log at
        # ERROR and return the failure so the caller can surface it (do NOT swallow).
        logger.error("Failed to bank lemma %s: %s", point_id, exc)
        return {"ok": False, "id": point_id, "error": str(exc)}


def retrieve_lemmas(goal: str, limit: int = 5) -> list[dict[str, Any]]:
    """Retrieve banked lemmas most similar to ``goal``, ranked by vector score.

    Fail-soft read: any failure logs a warning and returns ``[]`` (a degraded
    retrieval must never break a run — Path B synthesis can still close the goal).

    Args:
        goal: The current goal / sub-goal to find applicable lemmas for.
        limit: Maximum candidates to return.

    Returns:
        Payload dicts (``statement``/``proof_term``/``generality_score``/
        ``source_goal``/``verified_at``/``verification_mode``) plus a rounded
        ``score``, ordered by descending vector score. Empty on any failure.
    """
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
        return [
            {
                "statement": h.payload.get("statement", ""),
                "proof_term": h.payload.get("proof_term", ""),
                "generality_score": h.payload.get("generality_score", 0.0),
                "source_goal": h.payload.get("source_goal", ""),
                "verified_at": h.payload.get("verified_at"),
                "verification_mode": h.payload.get("verification_mode"),
                "lean_type": h.payload.get("lean_type"),
                "score": round(h.score, 3),
            }
            for h in response.points
        ]
    except Exception as exc:
        logger.warning("Failed to retrieve lemmas: %s", exc)
        return []
