"""
title: Second Brain
author: Charlie Tolleson
version: 1.0.0
license: MIT
required_open_webui_version: 0.4.0
requirements: httpx==0.27.0
description: Search your Obsidian second brain or load full project context by name.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from pydantic import BaseModel, Field


"""
Second Brain — Open WebUI tool

Purpose:
    Give every model in OWUI the same second-brain access that Claude Code gets via
    CLAUDE.md auto-injection. Two tools:

      search(query)       — semantic search with parent-child retrieval (no reranking
                            here; heavy cross-encoder lives host-side in qdrant_client.py)
      load_project(name)  — load all indexed content for a named project, CLAUDE.md first

Implementation:
    - Uses Qdrant REST API and LiteLLM /embeddings directly via httpx (no heavy deps).
    - Qdrant and LiteLLM URLs are Valves — set to Docker service names when running
      inside the OWUI container (qdrant:6333, litellm:4000).
    - Searches child chunks for precision, fetches parent chunks for rich context.
    - load_project scrolls all parent chunks filtered by project tag, sorted CLAUDE.md first.
"""


EMBED_MODEL = "text-embedding-3-small"
COLLECTION  = "second_brain"


class Tools:
    """Open WebUI tool plugin exposing second-brain retrieval to chat models.

    OWUI discovers the ``Tools`` class by convention and surfaces every public
    coroutine method (``search``, ``load_project``) as a callable tool to the LLM.
    Configuration is supplied through the nested :class:`Valves` model, which OWUI
    renders as an admin settings form and injects as ``self.valves``.

    Design notes:
        - All network I/O (Qdrant REST, LiteLLM embeddings) is done with httpx so
          the plugin has no heavy dependencies inside the OWUI container.
        - No reranking happens here; the cross-encoder reranker lives host-side in
          ``agents/_shared/qdrant_client.py``. This tool relies on parent-child
          retrieval (search precise child chunks, return rich parent chunks).
    """

    class Valves(BaseModel):
        """Admin-configurable settings for the Second Brain tool.

        Rendered by Open WebUI as a settings form and injected onto the tool
        instance as ``self.valves``. Defaults assume the plugin runs inside the
        OWUI Docker container, where Qdrant and LiteLLM are reachable by their
        Compose service names.
        """

        qdrant_url: str = Field(
            default="http://qdrant:6333",
            description="Qdrant base URL (use http://qdrant:6333 inside Docker).",
        )
        litellm_url: str = Field(
            default="http://litellm:4000/v1",
            description="LiteLLM base URL for embeddings (use http://litellm:4000/v1 inside Docker).",
        )
        litellm_key: str = Field(
            default="",
            description="LiteLLM master key (copy from ~/ai/ai-router/.env).",
        )
        score_threshold: float = Field(
            default=0.35,
            description="Minimum cosine similarity for initial retrieval (0–1).",
        )
        search_limit: int = Field(
            default=5,
            description="Number of results returned by search().",
        )

    def __init__(self) -> None:
        """Initialise the tool with default Valves.

        OWUI overwrites ``self.valves`` with admin-saved values after
        construction, so defaults here only apply until the form is configured.
        """
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        """Embed ``text`` into a vector via the LiteLLM /embeddings endpoint.

        Args:
            text: Natural-language text to embed.

        Returns:
            The embedding vector as a list of floats.

        Raises:
            httpx.HTTPStatusError: If the embeddings request returns a non-2xx
                status (e.g. bad/missing ``litellm_key``).
        """
        # Routed through LiteLLM (per repo convention) rather than the provider directly.
        url = self.valves.litellm_url.rstrip("/") + "/embeddings"
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {self.valves.litellm_key}"},
                json={"model": EMBED_MODEL, "input": text},
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]

    def _qdrant_search(self, vector: list[float], filt: dict, limit: int) -> list[dict]:
        """Run a vector similarity search against the second_brain collection.

        Args:
            vector: Query embedding to search by.
            filt: Qdrant filter dict (e.g. restrict to child chunks/project); may
                be empty to apply no filter.
            limit: Maximum number of scored points to return.

        Returns:
            List of Qdrant point dicts (each with ``id``, ``score``, ``payload``),
            already filtered by ``score_threshold``.

        Raises:
            httpx.HTTPStatusError: On a non-2xx response from Qdrant.
        """
        url = f"{self.valves.qdrant_url.rstrip('/')}/collections/{COLLECTION}/points/search"
        payload: dict[str, Any] = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "score_threshold": self.valves.score_threshold,
        }
        if filt:
            payload["filter"] = filt
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json().get("result", [])

    def _qdrant_retrieve(self, ids: list[str]) -> list[dict]:
        """Retrieve points by ID from Qdrant.

        Used to fetch parent chunks once their IDs are known from a child-chunk
        search, so the model gets full-context parents rather than fragments.

        Args:
            ids: Point IDs to fetch. An empty list short-circuits to ``[]``.

        Returns:
            List of point dicts with payloads (empty if ``ids`` is empty).

        Raises:
            httpx.HTTPStatusError: On a non-2xx response from Qdrant.
        """
        if not ids:
            return []
        url = f"{self.valves.qdrant_url.rstrip('/')}/collections/{COLLECTION}/points"
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json={"ids": ids, "with_payload": True})
            resp.raise_for_status()
            return resp.json().get("result", [])

    def _qdrant_scroll(self, filt: dict, limit: int = 50) -> list[dict]:
        """Scroll (paginate) through all points matching a filter.

        Unlike :meth:`_qdrant_search`, this does no vector matching — it walks the
        full set of points satisfying ``filt`` using Qdrant's cursor pagination.
        Used by ``load_project`` to gather every parent chunk for a project.

        Args:
            filt: Qdrant filter dict; may be empty to scroll the whole collection.
            limit: Page size per request (not a total cap; all pages are collected).

        Returns:
            All matching point dicts across every page.

        Raises:
            httpx.HTTPStatusError: On a non-2xx response from Qdrant.
        """
        url = f"{self.valves.qdrant_url.rstrip('/')}/collections/{COLLECTION}/points/scroll"
        payload: dict[str, Any] = {"limit": limit, "with_payload": True}
        if filt:
            payload["filter"] = filt
        points: list[dict] = []
        next_offset = None
        with httpx.Client(timeout=30.0) as client:
            # Follow Qdrant's next_page_offset cursor until exhausted.
            while True:
                if next_offset is not None:
                    payload["offset"] = next_offset
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json().get("result", {})
                batch = data.get("points", [])
                points.extend(batch)
                next_offset = data.get("next_page_offset")
                # Stop when there is no further page or the page came back empty.
                if next_offset is None or not batch:
                    break
        return points

    def _child_filter(
        self,
        project: str | None = None,
        para: str | None = None,
    ) -> dict:
        """Build a Qdrant filter that restricts results to child chunks.

        Child chunks are the smaller, more precise segments searched against;
        their parents are fetched afterwards for context. Optional project / PARA
        constraints are matched case-insensitively (values lowercased).

        Args:
            project: Optional project tag to require (e.g. "hyperion").
            para: Optional PARA category to require ("project"/"area"/...).

        Returns:
            A Qdrant filter dict of the form ``{"must": [...]}``.
        """
        must = [{"key": "chunk_type", "match": {"value": "child"}}]
        # Tags are stored lowercased at ingest time, so match lowercased here.
        if project:
            must.append({"key": "project", "match": {"value": project.lower()}})
        if para:
            must.append({"key": "para_category", "match": {"value": para.lower()}})
        return {"must": must}

    def _format_results(self, candidates: list[dict]) -> str:
        """Render ranked candidates into a Markdown context block for the model.

        Args:
            candidates: Result dicts from :meth:`search`, each with keys such as
                ``title``, ``text``, ``para_category``, ``project``,
                ``section_header`` and ``score``.

        Returns:
            A Markdown string with one section per candidate, or a "no results"
            placeholder when ``candidates`` is empty.
        """
        if not candidates:
            return "(No relevant notes found in second brain.)"
        lines = ["## Relevant notes from second brain\n"]
        for r in candidates:
            label = (r.get("para_category") or "note").upper()
            proj  = f" · {r['project']}" if r.get("project") else ""
            hdr   = r.get("section_header", "")
            score = r.get("score", "")
            lines.append(
                f"### [{label}{proj}] {r.get('title', '')} "
                + (f"— {hdr} " if hdr else "")
                + (f"(relevance: {score:.3f})" if isinstance(score, float) else "")
            )
            lines.append(r.get("text", ""))
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Public tools
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        project: str = "",
        para_category: str = "",
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """Search the second brain for notes relevant to a query.

        Use this proactively whenever the user asks about their projects, past work,
        career notes, investment research, or anything that might be in personal notes.
        Do NOT use for general knowledge questions.

        Args:
            query:         What to search for (natural language).
            project:       Optional project filter, e.g. "hyperion". Leave blank for all.
            para_category: Optional PARA filter: "project", "area", "resource", or "archive".

        Returns:
            Formatted context block with matching note sections and their source files.
        """
        async def emit(desc: str, done: bool):
            """Send a status update to the OWUI UI if an event emitter is provided.

            Args:
                desc: Human-readable status text.
                done: Whether this is the terminal status for the operation.
            """
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": desc, "done": done}})

        try:
            await emit(f"Searching second brain: {query!r}", False)
            vector = self._embed(query)
            filt = self._child_filter(
                project=project or None,
                para=para_category or None,
            )
            # Over-fetch child hits (20) then dedup to parents; final cut is search_limit.
            hits = self._qdrant_search(vector, filt, limit=20)

            if not hits:
                await emit("No results found", True)
                return "(No relevant notes found in second brain.)"

            await emit(f"Found {len(hits)} candidate(s), fetching context…", False)
            # Fetch parents for rich context, dedup by parent_point_id
            parent_ids = list({
                h["payload"].get("parent_point_id")
                for h in hits
                if h["payload"].get("parent_point_id")
            })
            parent_map = {
                str(pt["id"]): pt["payload"]
                for pt in self._qdrant_retrieve(parent_ids)
            }

            candidates: list[dict] = []
            seen: set[str] = set()
            for hit in hits:
                pid = hit["payload"].get("parent_point_id", "")
                # Each parent is emitted once (first/highest-scoring child wins);
                # the child's score is kept as the candidate's relevance.
                if pid and pid not in seen:
                    seen.add(pid)
                    parent = parent_map.get(pid, {})
                    candidates.append({
                        "title":          parent.get("title") or hit["payload"].get("title", ""),
                        "text":           parent.get("text") or hit["payload"].get("text", ""),
                        "file_path":      hit["payload"].get("file_path", ""),
                        "para_category":  hit["payload"].get("para_category", ""),
                        "project":        hit["payload"].get("project") or "",
                        "section_header": hit["payload"].get("section_header", ""),
                        "score":          hit.get("score", 0.0),
                    })
                # Fallback for chunks that have no parent link but carry text inline.
                elif not pid and hit["payload"].get("text"):
                    candidates.append({
                        "title":          hit["payload"].get("title", ""),
                        "text":           hit["payload"].get("text", ""),
                        "file_path":      hit["payload"].get("file_path", ""),
                        "para_category":  hit["payload"].get("para_category", ""),
                        "project":        hit["payload"].get("project") or "",
                        "section_header": hit["payload"].get("section_header", ""),
                        "score":          hit.get("score", 0.0),
                    })

            candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
            top = candidates[: self.valves.search_limit]
            await emit(f"Returning {len(top)} result(s)", True)
            return self._format_results(top)

        except Exception as e:
            await emit(f"Search failed: {e}", True)
            return f"[second_brain.search error] {e}"

    async def load_project(
        self,
        name: str,
        __event_emitter__: Callable[[dict], Any] | None = None,
    ) -> str:
        """Load all indexed notes for a named project from the second brain.

        Call this when the user says "load the X project", "give me context on X",
        or "what's the status of X". Returns the project's CLAUDE.md first (goals,
        status, gotchas), followed by other notes in that project folder.

        Args:
            name: Project name, e.g. "hyperion", "ResearchAgent".

        Returns:
            Full project context formatted as a readable document.
        """
        async def emit(desc: str, done: bool):
            """Send a status update to the OWUI UI if an event emitter is provided.

            Args:
                desc: Human-readable status text.
                done: Whether this is the terminal status for the operation.
            """
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": desc, "done": done}})

        try:
            await emit(f"Loading project: {name}", False)
            filt = {
                "must": [
                    {"key": "chunk_type", "match": {"value": "parent"}},
                    {"key": "project",    "match": {"value": name.lower()}},
                ]
            }
            points = self._qdrant_scroll(filt, limit=100)

            if not points:
                await emit(f"No content found for '{name}'", True)
                return f"(No indexed content found for project '{name}'. Run ingest_obsidian.py to index it.)"

            sections = [
                {
                    "title":          pt["payload"].get("title", ""),
                    "text":           pt["payload"].get("text", ""),
                    "file_path":      pt["payload"].get("file_path", ""),
                    "section_header": pt["payload"].get("section_header", ""),
                }
                for pt in points
                if pt.get("payload", {}).get("text")
            ]

            # Sort: CLAUDE.md first, then alphabetically
            sections.sort(key=lambda s: (
                0 if s["file_path"].upper().endswith("CLAUDE.MD") else 1,
                s["file_path"],
                s["section_header"],
            ))

            lines = [f"## Project: {name}\n"]
            current_file = None
            for s in sections:
                if s["file_path"] != current_file:
                    current_file = s["file_path"]
                    lines.append(f"\n### {current_file}")
                if s["section_header"]:
                    lines.append(f"\n{s['section_header']}")
                lines.append(s["text"])

            await emit(f"Loaded {len(sections)} section(s) for '{name}'", True)
            return "\n".join(lines)

        except Exception as e:
            await emit(f"Load failed: {e}", True)
            return f"[second_brain.load_project error] {e}"
