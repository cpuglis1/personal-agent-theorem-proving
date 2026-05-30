"""Reranker tool — thin client for Infinity bge-reranker-v2-m3."""

from __future__ import annotations

import logging

import httpx

from hyperion.config import settings

logger = logging.getLogger(__name__)

_MODEL = "BAAI/bge-reranker-v2-m3"


def rerank(query: str, documents: list[str], top_n: int = 5) -> list[tuple[int, float]]:
    """
    Rerank documents against a query.

    Returns list of (original_index, score) sorted descending by score,
    capped at top_n.
    """
    if not documents:
        return []
    try:
        resp = httpx.post(
            f"{settings.infinity_url}/rerank",
            json={"model": _MODEL, "query": query, "documents": documents},
            timeout=15.0,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        ranked = sorted(results, key=lambda r: r["relevance_score"], reverse=True)
        return [(r["index"], r["relevance_score"]) for r in ranked[:top_n]]
    except Exception as exc:
        logger.warning("Reranker unavailable (%s) — returning original order", exc)
        return [(i, 0.0) for i in range(min(top_n, len(documents)))]


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (≈4 chars/token) — good enough for budget trimming."""
    return max(1, len(text) // 4)


def prioritize(
    query: str,
    candidates: list[str],
    top_n: int | None = None,
    token_budget: int | None = None,
) -> list[str]:
    """Shared retrieval-prioritization primitive (PLAN_UNIFIED.md Phase 4).

    Reranks ``candidates`` by relevance to ``query`` (highest first), then trims the
    lowest-scored items so the running token estimate fits ``token_budget``. This is
    how the deferred per-stage input-token cap is finally enforced: instead of
    aborting when raw retrieval is too large, we keep the most relevant slice that
    fits and log what was dropped.

    Returns the kept candidate strings in ranked order.
    """
    if not candidates:
        return []
    limit = top_n if top_n is not None else len(candidates)
    ranked = rerank(query, candidates, top_n=min(limit, len(candidates)))

    kept: list[str] = []
    used = 0
    dropped = 0
    for idx, _score in ranked:
        text = candidates[idx]
        if token_budget is not None:
            cost = _estimate_tokens(text)
            if used + cost > token_budget and kept:
                dropped += 1
                continue
            used += cost
        kept.append(text)
    if dropped:
        logger.info(
            "prioritize: trimmed %d/%d candidates to fit token_budget=%s (≈%d tokens kept)",
            dropped, len(candidates), token_budget, used,
        )
    return kept
