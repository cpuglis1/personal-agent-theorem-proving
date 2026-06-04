"""
title: Notion
author: Charlie Tolleson
version: 0.2.0
license: MIT
required_open_webui_version: 0.4.0
requirements: notion-client==2.2.1
description: Search pages and databases, create pages from markdown, append blocks to existing pages, and update page properties via the Notion API.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from notion_client import Client
from notion_client.errors import APIResponseError, RequestTimeoutError
from pydantic import BaseModel, Field


"""
Notion — Open WebUI tool
========================

Role in the system:
    This module is an **Open WebUI (OWUI) tool plugin**. OWUI loads the file,
    reads the frontmatter docstring at the very top of the file (the `title`/
    `author`/`requirements`/... block — which MUST remain the first statement so
    OWUI's metadata parser and dependency installer can find it), instantiates
    the `Tools` class, and exposes each public method on that class as a callable
    "tool" that chat models can invoke during a conversation. It is the Notion
    read/write surface for the broader ~/ai ecosystem's chat models.

Purpose:
    Lets chat models read and write the user's Notion workspace: searching for
    pages and databases, creating new pages with rich content, appending content
    to existing pages, and updating structured properties (e.g. Status, Tags).

Public tool surface (methods of `Tools` exposed to the model):
    - search_notion(query)                          → find pages/databases
    - create_notion_page(title, markdown, parent?)  → new page from markdown
    - append_to_page(page_id, markdown)             → add blocks to a page
    - update_page_properties(page_id, props_json)   → set native properties

Return contract:
    Every tool returns a *string*. On success it is a JSON document; on failure
    it is a human-readable "ERROR: ..." string rather than a raised exception, so
    the calling model always receives a usable text result and can react to the
    error instead of the tool call aborting. (The exception is invalid-JSON input
    to update_page_properties, which is also reported as an "ERROR: ..." string.)

Implementation notes / non-obvious context:
    - Uses the official notion-client library (pinned to 2.2.1) declared in
      frontmatter requirements so Open WebUI installs it on first load.
    - Auth token is stored in the Notion Valve (admin-set in the OWUI UI) with
      an env-var fallback (NOTION_API_KEY) for first boot. Never hard-coded.
    - Markdown is converted to Notion blocks by a minimal hand-written parser
      supporting headings (h1–h3), bullet/numbered lists, fenced code blocks,
      and paragraphs. Inline formatting is passed through as plain text.
    - Notion's API limit of 100 blocks per append call is respected; large pages
      are sent in batches automatically.
    - Rich-text chunks are capped at 1,900 chars (Notion's 2,000-char limit with
      a safety margin) to avoid silent truncation.
    - create_notion_page auto-detects whether the parent ID is a database or a
      page (one extra round-trip) to pick the right properties shape and avoid
      the most common 400 error.
"""


_NOTION_VERSION = "2022-06-28"
_BLOCK_CHUNK = 100                  # Notion API caps appended blocks per call


def _markdown_to_blocks(md: str) -> list[dict]:
    """
    Minimal markdown → Notion block converter.

    Supports: # / ## / ### headings, bullet (- / *) and numbered (1.) lists,
    fenced ```code``` blocks, and paragraphs. Inline formatting is passed through
    as plain text — Notion's rich_text grammar isn't worth re-implementing here.

    Parsing notes:
        - Headings are matched ### → ## → # so the longer prefixes win before the
          shorter "# " prefix would greedily match them.
        - Blank lines are skipped (Notion has no explicit blank-line block).
        - Anything not matching a known pattern falls through to a paragraph.

    :param md: Source markdown text.
    :return: A list of Notion block dicts, in document order, ready to pass as
             ``children`` (subject to the 100-block-per-call API limit, handled
             by callers).
    """
    lines = md.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Fenced code block
        m = re.match(r"^```([a-zA-Z0-9_+-]*)\s*$", line)
        if m:
            lang = m.group(1) or "plain text"
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1  # skip closing fence
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(buf)[:1900]}}],
                    "language": lang.lower() if lang else "plain text",
                },
            })
            continue

        if line.startswith("### "):
            blocks.append(_heading(3, line[4:])); i += 1; continue
        if line.startswith("## "):
            blocks.append(_heading(2, line[3:])); i += 1; continue
        if line.startswith("# "):
            blocks.append(_heading(1, line[2:])); i += 1; continue

        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            blocks.append(_bullet(m.group(1))); i += 1; continue

        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            blocks.append(_numbered(m.group(1))); i += 1; continue

        if line.strip() == "":
            i += 1; continue

        blocks.append(_paragraph(line)); i += 1

    return blocks


def _rt(text: str) -> list[dict]:
    """
    Wrap a plain string in Notion's ``rich_text`` array shape (a single text run).

    :param text: The text content. Truncated to 1,900 chars (Notion caps a single
                 rich_text chunk at 2,000; the margin avoids edge-case 400s).
    :return: A one-element list suitable for any block's ``rich_text`` field.
    """
    # Notion caps rich_text content at 2000 chars per chunk.
    return [{"type": "text", "text": {"content": text[:1900]}}]

def _heading(level: int, text: str) -> dict:
    """
    Build a Notion ``heading_{level}`` block.

    :param level: Heading level 1–3 (maps to Notion heading_1/heading_2/heading_3).
    :param text: Heading text.
    :return: A Notion block dict.
    """
    return {"object": "block", "type": f"heading_{level}",
            f"heading_{level}": {"rich_text": _rt(text)}}

def _paragraph(text: str) -> dict:
    """
    Build a Notion ``paragraph`` block from a line of text.

    :param text: Paragraph text.
    :return: A Notion block dict.
    """
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rt(text)}}

def _bullet(text: str) -> dict:
    """
    Build a Notion ``bulleted_list_item`` block.

    :param text: List item text (the content after the ``-``/``*`` marker).
    :return: A Notion block dict.
    """
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(text)}}

def _numbered(text: str) -> dict:
    """
    Build a Notion ``numbered_list_item`` block.

    Note: Notion renumbers numbered list items automatically, so the original
    markdown number (e.g. ``1.``) is intentionally discarded — only the item
    text is preserved.

    :param text: List item text (the content after the ``N.`` marker).
    :return: A Notion block dict.
    """
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": _rt(text)}}


class Tools:
    """
    Open WebUI tool container.

    OWUI discovers this class by name (``Tools``), instantiates it once, and
    surfaces each public method as a model-callable tool. Configuration lives in
    the nested ``Valves`` model, which OWUI renders as admin-editable settings.

    Instance attributes:
        valves:   Populated ``Valves`` instance holding the Notion token, default
                  parent database ID, and search page size.
        citation: When True, OWUI attaches source citations for tool output.
    """

    class Valves(BaseModel):
        """
        Admin-configurable settings for this tool, rendered in the OWUI UI.

        Fields:
            notion_token: Notion Internal Integration Token (defaults to the
                NOTION_API_KEY env var on first boot). The integration must be
                invited to every page/database it will touch.
            default_parent_database_id: Fallback parent database for
                create_notion_page when no parent_id is supplied.
            search_page_size: Maximum number of search hits to return (1–50).
        """

        notion_token: str = Field(
            default=os.environ.get("NOTION_API_KEY", ""),
            description="Notion Internal Integration Token. The integration must be invited "
                        "to every database/page you want to touch (··· → Connections).",
        )
        default_parent_database_id: str = Field(
            default="f467ff6f772044cda727acfef0d778aa",   # 💻 Projects
            description="Database used by create_notion_page when no parent_id is given.",
        )
        search_page_size: int = Field(default=10, ge=1, le=50,
            description="Max search results to return.")

    def __init__(self) -> None:
        """Instantiate the tool: load default Valves and enable OWUI citations."""
        self.valves = self.Valves()
        self.citation = True

    def _client(self) -> Client:
        """
        Build an authenticated notion-client ``Client`` from the current Valves.

        A fresh client is created per call (cheap; keeps the latest valve token).

        :return: A configured Notion ``Client`` pinned to the API version.
        :raises RuntimeError: If no Notion token is configured (valve unset and
                              NOTION_API_KEY env var absent).
        """
        if not self.valves.notion_token:
            raise RuntimeError("NOTION_API_KEY is not configured. Set the Notion valve in OWUI admin.")
        return Client(auth=self.valves.notion_token, notion_version=_NOTION_VERSION)

    # ------------------------------------------------------------------ tools

    def search_notion(self, query: str) -> str:
        """
        Search the Notion workspace for pages and databases matching a query.

        :param query: Free-text search query (Notion does prefix and substring matching).
        :return: JSON array of hits: [{"id": str, "title": str, "url": str, "type": str}, ...]
        """
        try:
            notion = self._client()
            res = notion.search(query=query, page_size=self.valves.search_page_size)
            hits = []
            for r in res.get("results", []):
                title = ""
                if r["object"] == "page":
                    # Title lives in the property whose type is "title".
                    for prop in r.get("properties", {}).values():
                        if prop.get("type") == "title" and prop["title"]:
                            title = "".join(t["plain_text"] for t in prop["title"]); break
                elif r["object"] == "database":
                    title = "".join(t["plain_text"] for t in r.get("title", []))
                hits.append({
                    "id": r["id"],
                    "title": title or "(untitled)",
                    "url": r.get("url", ""),
                    "type": r["object"],
                })
            return json.dumps(hits, indent=2)
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: search_notion failed: {e!r}"

    def create_notion_page(
        self,
        title: str,
        markdown_content: str,
        parent_id: Optional[str] = None,
    ) -> str:
        """
        Create a new Notion page from markdown content.

        :param title: Page title.
        :param markdown_content: Page body in markdown. Converted to Notion blocks.
        :param parent_id: Optional Notion page or database ID. If omitted, the page is created
                          in the admin-configured default database (currently 💻 Projects).
        :return: JSON {"id": str, "url": str, "title": str} or an ERROR message.
        """
        try:
            notion = self._client()
            parent_id = parent_id or self.valves.default_parent_database_id

            # Determine whether the parent is a page or a database — one extra round trip,
            # but it removes the most common source of 400-errors ("invalid parent").
            try:
                notion.databases.retrieve(database_id=parent_id)
                parent = {"database_id": parent_id}
                properties = {"Name": {"title": [{"text": {"content": title[:200]}}]}}
            except APIResponseError:
                notion.pages.retrieve(page_id=parent_id)
                parent = {"page_id": parent_id}
                properties = {"title": [{"text": {"content": title[:200]}}]}

            blocks = _markdown_to_blocks(markdown_content)
            first_batch = blocks[:_BLOCK_CHUNK]
            rest = blocks[_BLOCK_CHUNK:]

            page = notion.pages.create(parent=parent, properties=properties, children=first_batch)

            # Append remaining blocks in 100-block batches (API limit).
            for start in range(0, len(rest), _BLOCK_CHUNK):
                notion.blocks.children.append(
                    block_id=page["id"],
                    children=rest[start:start + _BLOCK_CHUNK],
                )

            return json.dumps({"id": page["id"], "url": page.get("url", ""), "title": title})
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: create_notion_page failed: {e!r}"

    def append_to_page(self, page_id: str, markdown_content: str) -> str:
        """
        Append markdown content as new blocks to an existing Notion page.

        :param page_id: Notion page ID (with or without hyphens).
        :param markdown_content: Markdown to append. Converted to Notion blocks.
        :return: JSON {"appended_blocks": int} or an ERROR message.
        """
        try:
            notion = self._client()
            blocks = _markdown_to_blocks(markdown_content)
            appended = 0
            for start in range(0, len(blocks), _BLOCK_CHUNK):
                batch = blocks[start:start + _BLOCK_CHUNK]
                notion.blocks.children.append(block_id=page_id, children=batch)
                appended += len(batch)
            return json.dumps({"appended_blocks": appended, "page_id": page_id})
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: append_to_page failed: {e!r}"

    def update_page_properties(self, page_id: str, properties_json: str) -> str:
        """
        Update one or more native properties on an existing Notion page (e.g. Status, Tags).

        :param page_id: Notion page ID.
        :param properties_json: JSON object matching Notion's properties payload, e.g.
                                '{"Status": {"select": {"name": "In progress"}}}'.
        :return: JSON {"id": str, "url": str} or an ERROR message.
        """
        try:
            props = json.loads(properties_json)
        except json.JSONDecodeError as e:
            return f"ERROR: properties_json is not valid JSON: {e}"
        try:
            page = self._client().pages.update(page_id=page_id, properties=props)
            return json.dumps({"id": page["id"], "url": page.get("url", "")})
        except (APIResponseError, RequestTimeoutError) as e:
            return f"ERROR: notion API: {e}"
        except Exception as e:
            return f"ERROR: update_page_properties failed: {e!r}"
