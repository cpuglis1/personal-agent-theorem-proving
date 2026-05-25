"""
qdrant_client.py — Shared Qdrant helper for ~/ai/agents.

Usage:
    from _shared.qdrant_client import search_second_brain
    results = search_second_brain("investment thesis for SaaS companies")
"""

import os
from openai import OpenAI
from qdrant_client import QdrantClient as _QdrantClient
from qdrant_client.models import Filter

QDRANT_URL       = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION       = os.getenv("QDRANT_COLLECTION", "second_brain")
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1")
LITELLM_KEY      = os.getenv("LITELLM_MASTER_KEY", "")
EMBED_MODEL      = "text-embedding-3-small"

_qdrant = _QdrantClient(url=QDRANT_URL)
_oai    = OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_KEY)


def _embed(text: str) -> list[float]:
    return _oai.embeddings.create(model=EMBED_MODEL, input=text).data[0].embedding


def search_second_brain(
    query: str,
    limit: int = 5,
    score_threshold: float = 0.30,
    filter: Filter | None = None,
) -> list[dict]:
    """
    Semantic search over the second_brain Qdrant collection.

    Returns a list of dicts with keys:
        title, text (preview), notion_url, score
    """
    vector = _embed(query)
    hits = _qdrant.search(
        collection_name=COLLECTION,
        query_vector=vector,
        limit=limit,
        score_threshold=score_threshold,
        query_filter=filter,
        with_payload=True,
    )
    return [
        {
            "title":      h.payload.get("title", ""),
            "text":       h.payload.get("text", ""),
            "notion_url": h.payload.get("notion_url", ""),
            "score":      round(h.score, 3),
        }
        for h in hits
    ]


def format_context(results: list[dict]) -> str:
    """Format search results into a context block suitable for an LLM prompt."""
    if not results:
        return "(No relevant notes found in second brain.)"
    lines = ["## Relevant notes from second brain\n"]
    for r in results:
        lines.append(f"### {r['title']} (relevance: {r['score']})")
        lines.append(r["text"])
        lines.append(f"Source: {r['notion_url']}\n")
    return "\n".join(lines)
