"""
notion.py — write a task's final artifact to Notion (Phase 9 follow-up affordance).

Backs the "save to Notion" follow-up the Synthesizer offers. Talks to the Notion
REST API directly via httpx (no extra SDK dependency). Requires NOTION_API_KEY and
NOTION_DATABASE_ID in the environment; absent either, calls return a clear error
string rather than raising, so an agent loop degrades gracefully.
"""

from __future__ import annotations

import httpx
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from hyperion.config import settings

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

# Notion rejects rich-text blocks longer than 2000 chars; chunk the body to fit.
_BLOCK_LIMIT = 1900


def _paragraph_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    for para in text.split("\n"):
        chunk = para or ""
        while True:
            head, chunk = chunk[:_BLOCK_LIMIT], chunk[_BLOCK_LIMIT:]
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": head}}]},
                }
            )
            if not chunk:
                break
    return blocks[:100]  # Notion caps children at 100 per create call


def create_notion_page(title: str, body: str) -> dict:
    """Create a page in the configured database. Returns {"url": ...} or {"error": ...}."""
    if not settings.notion_api_key or not settings.notion_database_id:
        return {"error": "Notion not configured (set NOTION_API_KEY and NOTION_DATABASE_ID)"}

    headers = {
        "Authorization": f"Bearer {settings.notion_api_key}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"database_id": settings.notion_database_id},
        "properties": {"title": {"title": [{"text": {"content": title[:200]}}]}},
        "children": _paragraph_blocks(body),
    }
    try:
        resp = httpx.post(f"{_NOTION_API}/pages", headers=headers, json=payload, timeout=20.0)
    except Exception as exc:
        return {"error": f"Notion request failed: {exc}"}
    if resp.status_code >= 300:
        return {"error": f"Notion API HTTP {resp.status_code}: {resp.text[:300]}"}
    data = resp.json()
    return {"url": data.get("url"), "id": data.get("id")}


class _NotionWriteInput(BaseModel):
    title: str = Field(description="Title for the new Notion page")
    body: str = Field(description="Markdown/plain-text body content for the page")


class NotionWriteTool(BaseTool):
    name: str = "notion_write"
    description: str = (
        "Save a result to the configured Notion database as a new page. "
        "Input: a title and a plain-text body. Returns the new page URL."
    )
    args_schema: type[BaseModel] = _NotionWriteInput

    def _run(self, title: str, body: str) -> str:
        result = create_notion_page(title, body)
        if "error" in result:
            return f"Notion write failed: {result['error']}"
        return f"Saved to Notion: {result.get('url')}"
