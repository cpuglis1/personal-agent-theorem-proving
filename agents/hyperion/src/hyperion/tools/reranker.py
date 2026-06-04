"""Reranker tool — thin client for the Infinity bge-reranker-v2-m3 service.

Purpose
-------
Provides cross-encoder reranking for Hyperion's retrieval pipeline. Given a query
and a list of candidate document strings, it asks the Infinity reranker service
(part of the shared ai-router Docker stack) to score how relevant each candidate
is to the query, then returns the candidates ordered best-first.

Role in the system
-------------------
Vector search (Qdrant) returns candidates by embedding similarity, which is fast
but coarse. This module adds a second, more precise ranking pass: the
``bge-reranker-v2-m3`` cross-encoder reads each (query, document) pair jointly and
produces a sharper relevance score. Agents/tools call :func:`prioritize` (or the
lower-level :func:`rerank`) to keep only the most relevant — and budget-fitting —
slice of retrieved context before feeding it to an LLM.

Key design decisions / non-obvious context
-------------------------------------------
- HTTP, not in-process: the model runs in the Infinity service (see
  ``settings.infinity_url``); this file is a thin synchronous ``httpx`` client.
  All LLM/model traffic goes through shared infra rather than loading models here.
- Fail-soft: if the reranker service is unreachable or errors, callers should
  still make progress. :func:`rerank` therefore degrades gracefully by returning
  the original ordering (with zero scores) instead of raising.
- Token-budget trimming: :func:`prioritize` enforces the deferred per-stage
  input-token cap (PLAN_UNIFIED.md Phase 4) by dropping the lowest-ranked
  candidates rather than aborting when retrieval is too large to fit.
"""

from __future__ import annotations

import logging

import httpx

from hyperion.config import settings

logger = logging.getLogger(__name__)

# Cross-encoder model name expected by the Infinity reranker service. Must match a
# model loaded/served by that service; changing it here without changing the
# server config will cause the rerank request to fail (caught and degraded below).
_MODEL = "BAAI/bge-reranker-v2-m3"


def rerank(query: str, documents: list[str], top_n: int = 5) -> list[tuple[int, float]]:
    """
    Rerank documents against a query using the Infinity cross-encoder.

    Sends a single ``/rerank`` request to the Infinity service and returns the
    candidate indices ordered by descending relevance.

    Args:
        query: The search/query string to score documents against.
        documents: Candidate document strings, in their original order. The
            integer indices in the result refer to positions in this list.
        top_n: Maximum number of results to return (the highest-scored slice).
            Defaults to 5.

    Returns:
        A list of ``(original_index, relevance_score)`` tuples sorted descending
        by score and capped at ``top_n``. ``original_index`` is the position of
        the document in the input ``documents`` list, so callers can map results
        back to the originals. Returns an empty list when ``documents`` is empty.

    Raises:
        None. Network/HTTP/parsing failures are caught and handled by degrading
        gracefully: a warning is logged and the original ordering is returned
        with placeholder ``0.0`` scores (capped at ``top_n``). This fail-soft
        behavior lets the retrieval pipeline keep working when the reranker
        service is down.

    Side effects:
        Performs a synchronous HTTP POST to ``settings.infinity_url`` (15s
        timeout) and may emit a warning log on failure.
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
        # Infinity returns each result with its own "index" (position in the
        # input list) and "relevance_score"; re-sort defensively in case the
        # service does not already return them best-first, then take top_n.
        ranked = sorted(results, key=lambda r: r["relevance_score"], reverse=True)
        return [(r["index"], r["relevance_score"]) for r in ranked[:top_n]]
    except Exception as exc:
        # Fail-soft: never propagate reranker outages to callers. Preserve the
        # original input order (first top_n items) so retrieval still proceeds.
        logger.warning("Reranker unavailable (%s) — returning original order", exc)
        return [(i, 0.0) for i in range(min(top_n, len(documents)))]


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (≈4 chars/token) — good enough for budget trimming.

    Uses the common heuristic of ~4 characters per token to avoid a real
    tokenizer dependency on this hot path. Intended only for approximate budget
    accounting in :func:`prioritize`, not for exact accounting/billing.

    Args:
        text: The text whose token count to estimate.

    Returns:
        Estimated token count as an int, floored at 1 so non-empty inputs always
        cost at least one token (avoids zero-cost items slipping past budgets).
    """
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

    Args:
        query: The query to rank ``candidates`` against.
        candidates: Candidate document strings to prioritize.
        top_n: Optional cap on how many candidates to consider after reranking.
            When ``None``, all candidates are considered.
        token_budget: Optional approximate token ceiling for the kept set. When
            set, candidates are added in ranked order until adding the next one
            would exceed the budget; lower-ranked overflow is dropped. When
            ``None``, no trimming occurs.

    Returns:
        The kept candidate strings (subset of ``candidates``) in best-first
        ranked order. Returns an empty list when ``candidates`` is empty.

    Raises:
        None directly. Delegates to :func:`rerank`, which is fail-soft (falls
        back to original order if the reranker service is unavailable).

    Side effects:
        Triggers a reranker HTTP call via :func:`rerank`, and emits an info log
        summarizing how many candidates were trimmed when ``token_budget`` causes
        any drops.
    """
    if not candidates:
        return []
    limit = top_n if top_n is not None else len(candidates)
    # Clamp top_n to the candidate count so we never request more than we have.
    ranked = rerank(query, candidates, top_n=min(limit, len(candidates)))

    kept: list[str] = []
    used = 0  # running estimated token total of kept items
    dropped = 0  # count of candidates skipped to stay within token_budget
    for idx, _score in ranked:
        text = candidates[idx]
        if token_budget is not None:
            cost = _estimate_tokens(text)
            # Skip this item if it would overflow the budget — but only once we
            # have kept at least one item ("and kept"), so the single most
            # relevant candidate is always returned even if it alone exceeds the
            # budget (better to return something than nothing).
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
