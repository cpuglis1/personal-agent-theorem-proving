"""Second brain tool — semantic search over second_brain Qdrant collection."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from crewai.tools import BaseTool
from pydantic import Field

# Load agents/_shared/qdrant_client.py via importlib to avoid name collision
# with the installed `qdrant-client` package.
# Local: parents[4] == agents/
# Docker: parents[4] == / (build context is agents/, so _shared/ lands at /app/_shared/)
_SHARED_PATH = Path(__file__).parents[4] / "_shared" / "qdrant_client.py"
if not _SHARED_PATH.exists():
    _SHARED_PATH = Path(__file__).parents[3] / "_shared" / "qdrant_client.py"
_spec = importlib.util.spec_from_file_location("hyperion_shared_qdrant", _SHARED_PATH)
_shared_qdrant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shared_qdrant)
search_second_brain = _shared_qdrant.search_second_brain

from hyperion.config import settings
from hyperion.tools.reranker import rerank


# Per-call retrieval budget (tokens). Keeps a single tool call from dumping more
# context than a stage can absorb — the shared input cap, enforced at the source.
_SECOND_BRAIN_TOKEN_BUDGET = 6000


class SecondBrainTool(BaseTool):
    name: str = "search_second_brain"
    description: str = (
        "Semantic search over the personal Notion second brain. "
        "Use for background knowledge, past notes, career goals, projects, and investments. "
        "Input: a natural-language query string."
    )
    top_k: int = Field(default=5, description="Number of results to return after reranking")

    def _run(self, query: str) -> str:
        # Fetch more candidates than needed, then rerank to top_k
        candidates = search_second_brain(
            query=query,
            limit=15,
            score_threshold=0.25,
        )
        if not candidates:
            return "(No relevant notes found in second brain.)"

        texts = [c["text"] for c in candidates]
        ranked = rerank(query, texts, top_n=self.top_k)

        lines = [f"## Second brain search results for: {query!r}\n"]
        used = 0
        for orig_idx, score in ranked:
            c = candidates[orig_idx]
            snippet = c["text"][:2000]  # cap per note
            cost = max(1, len(snippet) // 4)
            # Trim to the per-call token budget instead of dumping everything.
            if used and used + cost > _SECOND_BRAIN_TOKEN_BUDGET:
                lines.append(f"(…{len(ranked)} results trimmed to fit context budget)")
                break
            used += cost
            lines.append(f"### {c['title']} (relevance: {score:.3f})")
            lines.append(snippet)
            if c.get("notion_url"):
                lines.append(f"Source: {c['notion_url']}")
            lines.append("")
        return "\n".join(lines)
