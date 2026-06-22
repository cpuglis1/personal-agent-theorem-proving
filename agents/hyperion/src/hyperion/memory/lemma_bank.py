"""
Lemma bank — stores verified Lean lemmas in Qdrant ``skill_library``.

Role in the system
------------------
This is the prover's long-term "what have I already proved" memory and the engine of
the snowball: every verified lemma is embedded and persisted so future sub-goals can
*retrieve* it (Path A) instead of re-synthesizing it (Path B). It is a re-skin of
:mod:`hyperion.memory.episodic` (build plan Phase 2 / baseline §3 "tune") — same
Qdrant client, lazy imports, deterministic UUID5 upsert — with three changes:

1. **Embedding text** is the *lemma statement / goal type*, not ``request + summary``.
2. **Payload schema** is prover-native: ``statement``, ``lean_type``, ``proof_term``,
   ``normalized_key``, ``symbol_set``, provenance/counters, and verification metadata.
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


def _skill_collection() -> str:
    """Qdrant collection backing Hyperion's growing verified skill library."""
    return settings.qdrant_skill_library_collection


def _mathlib_collection() -> str:
    """Qdrant collection backing the static Mathlib premise corpus."""
    return settings.qdrant_mathlib_premises_collection


def _collection() -> str:
    """Backward-compatible alias for the current skill-library collection.

    Older callers/tests use ``_collection()`` to mean the proved-lemma bank. Keep that
    API while moving the semantics from a generic ``lemma_bank`` to ``skill_library``.
    """
    return _skill_collection()


def _get_clients():
    from openai import OpenAI
    from qdrant_client import QdrantClient

    oai = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
    qdrant = QdrantClient(url=settings.qdrant_url)
    return oai, qdrant


def _embed(oai, text: str) -> list[float]:
    return oai.embeddings.create(model="text-embedding-3-small", input=text).data[0].embedding


def _ensure_collection(qdrant, dims: int, collection: str | None = None) -> None:
    """Idempotently provision the lemma-bank collection before a write.

    Seed-Prover-style self-healing: the bank creates its backing Qdrant collection
    on first write (cosine, ``dims``-wide — derived from the live embedding so it can
    never drift from the model) instead of relying on a one-shot bootstrap script. A
    wiped/fresh Qdrant volume therefore self-provisions on the next banked lemma rather
    than silently 404-ing every ``store_lemma`` (the prior failure mode: the collection
    never existed, so the snowball could never start). No-op when it already exists.
    """
    from qdrant_client.models import Distance, VectorParams

    collection = collection or _collection()
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


_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*|[∀∃→↔=+\-*/<>≤≥]")
_BINDER_NAMES_RE = re.compile(r"[\(\{]\s*([A-Za-z_][A-Za-z0-9_']*)\s*:")
_LOCAL_STOPWORDS = {
    "theorem", "lemma", "example", "def", "instance", "abbrev", "by", "fun",
    "where", "let", "have", "show", "exact", "apply", "intro", "simp", "rw",
}


def symbol_set(text: str) -> list[str]:
    """Extract a small deterministic symbol set from a Lean statement/type.

    This is the sparse retrieval floor: constants/operators survive, obvious local
    binder names are dropped, and order is stable for payload diffs/tests. It is not a
    Lean parser; it is a cheap companion signal for dense retrieval.
    """
    binders = set(_BINDER_NAMES_RE.findall(text or ""))
    out: set[str] = set()
    for tok in _SYMBOL_RE.findall(text or ""):
        if tok in binders or tok in _LOCAL_STOPWORDS:
            continue
        if len(tok) == 1 and tok.islower():
            continue
        out.add(tok)
    return sorted(out)


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
    provenance: dict[str, Any] | None = None,
    times_retrieved: int = 0,
    times_won: int = 0,
    origin: str = "skill_library",
) -> StoreResult:
    """Embed and upsert a verified lemma into the bank.

    The write path is **loud** (risk #4): a failure logs at ``ERROR`` and returns
    ``StoreResult(ok=False, ...)`` so the caller can surface the lost lemma. It never
    raises — a banking failure must not kill an otherwise-good proof run.

    Args:
        statement: The lemma / goal type. Embedded for retrieval and normalized for
            the dedup identity.
        proof_term: The verified proof closing ``statement``.
        generality_score: Optional reuse score stored for retrieval/readout.
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
        doc_type = lean_type or statement
        vector = _embed(oai, doc_type)
        _ensure_collection(qdrant, len(vector))

        from qdrant_client.models import PointStruct

        payload: dict[str, Any] = {
            "statement": statement,
            "lean_type": doc_type,
            "proof_term": proof_term,
            "origin": origin,
            "source_collection": _skill_collection(),
            "normalized_key": _normalize(statement),
            "symbol_set": symbol_set(doc_type),
            "provenance": provenance or {"source_goal": source_goal},
            "times_retrieved": int(times_retrieved),
            "times_won": int(times_won),
            "generality_score": generality_score,
            "source_goal": source_goal,
            "verified_at": verified_at if verified_at is not None else int(time.time()),
            "verification_mode": verification_mode,
        }
        point = PointStruct(
            id=point_id,
            vector=vector,
            payload=payload,
        )
        collection = _skill_collection()
        qdrant.upsert(collection_name=collection, points=[point])
        logger.info("Banked lemma %s in %s", point_id, collection)
        return {"ok": True, "id": point_id, "error": None}
    except Exception as exc:
        # LOUD: a failed write loses a verified lemma and stalls the snowball. Log at
        # ERROR and return the failure so the caller can surface it (do NOT swallow).
        logger.error("Failed to bank lemma %s: %s", point_id, exc)
        return {"ok": False, "id": point_id, "error": str(exc)}


def _payload_from_hit(hit: Any) -> dict[str, Any]:
    payload = hit.payload or {}
    lean_type = payload.get("lean_type") or payload.get("statement", "")
    source_collection = payload.get("source_collection") or _skill_collection()
    return {
        "id": str(getattr(hit, "id", "") or ""),
        "statement": payload.get("statement", ""),
        "lean_type": lean_type,
        "proof_term": payload.get("proof_term", ""),
        "origin": payload.get("origin") or "skill_library",
        "source_collection": source_collection,
        "normalized_key": payload.get("normalized_key") or _normalize(payload.get("statement", "")),
        "symbol_set": payload.get("symbol_set") or symbol_set(lean_type),
        "provenance": payload.get("provenance") or {"source_goal": payload.get("source_goal", "")},
        "times_retrieved": int(payload.get("times_retrieved") or 0),
        "times_won": int(payload.get("times_won") or 0),
        "generality_score": payload.get("generality_score", 0.0),
        "source_goal": payload.get("source_goal", ""),
        "verified_at": payload.get("verified_at"),
        "verification_mode": payload.get("verification_mode"),
        "score": round(hit.score, 3),
    }


def _bump_times_retrieved(qdrant, lemmas: list[dict[str, Any]]) -> None:
    """Best-effort telemetry bump for thesis-curve retrieval counts."""
    updates = [
        (lem["id"], int(lem.get("times_retrieved") or 0) + 1)
        for lem in lemmas
        if lem.get("id") and lem.get("source_collection") == _skill_collection()
    ]
    if not updates:
        return
    try:
        for point_id, count in updates:
            qdrant.set_payload(
                collection_name=_collection(),
                payload={"times_retrieved": count},
                points=[point_id],
            )
    except Exception as exc:
        logger.warning("Failed to update lemma retrieval counters: %s", exc)


def bump_times_won(lemma: dict[str, Any]) -> int:
    """Best-effort telemetry bump when a banked lemma wins.

    Returns the incremented count so callers can carry it into a later ``store_lemma``
    upsert; that prevents banking the winning lemma from resetting the counter to zero.
    """
    count = int(lemma.get("times_won") or 0) + 1
    point_id = lemma.get("id")
    if not point_id or lemma.get("source_collection") not in (None, _skill_collection()):
        return count
    try:
        _oai, qdrant = _get_clients()
        qdrant.set_payload(
            collection_name=_collection(),
            payload={"times_won": count},
            points=[point_id],
        )
    except Exception as exc:
        logger.warning("Failed to update lemma win counter: %s", exc)
    return count


def retrieve_lemmas(goal: str, limit: int = 5) -> list[dict[str, Any]]:
    """Retrieve skill-library lemmas most similar to ``goal``, ranked by vector score.

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
        lemmas = [_payload_from_hit(h) for h in response.points]
        _bump_times_retrieved(qdrant, lemmas)
        return lemmas
    except Exception as exc:
        logger.warning("Failed to retrieve lemmas: %s", exc)
        return []


def retrieve_mathlib_premises(goal: str, limit: int = 5) -> list[dict[str, Any]]:
    """Retrieve static Mathlib premises most similar to ``goal``.

    This is the read-side interface for the future LeanDojo/Mathlib trace corpus. It is
    fail-soft and counter-free: Mathlib premises are plumbing, not the snowball skill
    library, so ``times_retrieved``/``times_won`` are intentionally not updated here.
    """
    try:
        oai, qdrant = _get_clients()
        vector = _embed(oai, goal)
        response = qdrant.query_points(
            collection_name=_mathlib_collection(),
            query=vector,
            limit=limit,
            score_threshold=_SCORE_THRESHOLD,
            with_payload=True,
        )
        out: list[dict[str, Any]] = []
        for hit in response.points:
            payload = hit.payload or {}
            lean_type = payload.get("lean_type") or payload.get("signature") or payload.get("statement", "")
            out.append({
                "id": str(getattr(hit, "id", "") or ""),
                "name": payload.get("name", ""),
                "statement": payload.get("statement") or payload.get("signature", ""),
                "lean_type": lean_type,
                "proof_term": payload.get("proof_term", ""),
                "origin": "mathlib",
                "source_collection": _mathlib_collection(),
                "normalized_key": payload.get("normalized_key") or _normalize(payload.get("statement", "")),
                "symbol_set": payload.get("symbol_set") or symbol_set(lean_type),
                "provenance": payload.get("provenance") or {"source": "mathlib"},
                "premises_used": payload.get("premises_used") or [],
                "score": round(hit.score, 3),
            })
        return out
    except Exception as exc:
        logger.warning("Failed to retrieve Mathlib premises: %s", exc)
        return []
