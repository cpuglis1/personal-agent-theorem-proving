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
    """Convert plain text into a list of Notion paragraph block objects.

    Splits the input on newlines so each line becomes one (or more) paragraph
    blocks, then further splits any line longer than ``_BLOCK_LIMIT`` into
    multiple blocks since Notion rejects rich-text content over 2000 chars.

    Args:
        text: The body text to render as Notion blocks. May contain newlines
            and arbitrarily long lines.

    Returns:
        A list of Notion block dicts (each a ``paragraph`` block), truncated to
        at most 100 entries because Notion caps ``children`` at 100 per
        page-create call. Excess content beyond 100 blocks is silently dropped.

    Notes:
        Empty lines are preserved as empty paragraph blocks (``chunk = para or ""``
        guarantees the inner loop runs at least once even for a blank line).
    """
    blocks: list[dict] = []
    for para in text.split("\n"):
        chunk = para or ""
        # Emit successive ``_BLOCK_LIMIT``-sized slices until the line is consumed.
        # Runs at least once even when ``chunk`` is empty (preserving blank lines).
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
    """Create a page in the configured Notion database from a title and body.

    Performs a synchronous POST to the Notion ``/pages`` endpoint, using the
    API key and database ID from :data:`hyperion.config.settings`. All error
    conditions (missing config, network failure, non-2xx HTTP) are returned as
    an ``{"error": ...}`` dict rather than raised, so the calling agent loop can
    degrade gracefully instead of crashing.

    Args:
        title: Page title. Truncated to the first 200 chars to stay within
            Notion's title limit.
        body: Plain-text/Markdown body, rendered into paragraph blocks via
            :func:`_paragraph_blocks`.

    Returns:
        On success, ``{"url": <page url>, "id": <page id>}``. On failure,
        ``{"error": <human-readable message>}``.

    Side effects:
        Issues a network request to the Notion REST API (20s timeout).
    """
    # Fail fast (without a network call) when credentials are not configured.
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
        # Catch-all so transport errors (DNS, timeout, connection reset) become
        # a returned error string rather than propagating up the agent loop.
        return {"error": f"Notion request failed: {exc}"}
    # Treat any non-2xx status as a failure; surface a truncated body for debugging.
    if resp.status_code >= 300:
        return {"error": f"Notion API HTTP {resp.status_code}: {resp.text[:300]}"}
    data = resp.json()
    return {"url": data.get("url"), "id": data.get("id")}


class _NotionWriteInput(BaseModel):
    """Pydantic argument schema for :class:`NotionWriteTool`.

    Defines the structured input the LLM must supply when invoking the
    ``notion_write`` tool; the field descriptions are surfaced to the model.
    """

    title: str = Field(description="Title for the new Notion page")
    body: str = Field(description="Markdown/plain-text body content for the page")


class NotionWriteTool(BaseTool):
    """CrewAI tool that saves a result to the configured Notion database.

    Wraps :func:`create_notion_page` so agents can persist a final artifact as a
    new Notion page. Exposed to the LLM as the ``notion_write`` tool with the
    schema defined by :class:`_NotionWriteInput`.
    """

    name: str = "notion_write"
    description: str = (
        "Save a result to the configured Notion database as a new page. "
        "Input: a title and a plain-text body. Returns the new page URL."
    )
    args_schema: type[BaseModel] = _NotionWriteInput

    def _run(self, title: str, body: str) -> str:
        """Execute the tool: create a Notion page and return a status string.

        Args:
            title: Title for the new page.
            body: Plain-text/Markdown body content.

        Returns:
            A human-readable success message containing the new page URL, or a
            ``"Notion write failed: ..."`` message if the underlying call
            returned an error. Never raises; errors are reported in the string.

        Side effects:
            Issues a network request to the Notion REST API via
            :func:`create_notion_page`.
        """
        result = create_notion_page(title, body)
        if "error" in result:
            return f"Notion write failed: {result['error']}"
        return f"Saved to Notion: {result.get('url')}"
