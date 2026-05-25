"""
notion_client.py — Shared Notion helper for ~/ai/agents.

Wraps the official notion-client library with convenience methods for the
common patterns used across agents (read page, append block, query DB).

Usage:
    from _shared.notion_client import NotionHelper
    n = NotionHelper()
    pages = n.query_database("your-db-id")
"""

import os
from notion_client import Client


class NotionHelper:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ.get("NOTION_API_KEY")
        if not key:
            raise ValueError("NOTION_API_KEY not set")
        self.client = Client(auth=key)

    def query_database(self, database_id: str, filter_: dict | None = None) -> list[dict]:
        """Return all pages from a database, handling pagination."""
        pages, cursor = [], None
        while True:
            kwargs: dict = {"database_id": database_id, "page_size": 100}
            if filter_:
                kwargs["filter"] = filter_
            if cursor:
                kwargs["start_cursor"] = cursor
            result = self.client.databases.query(**kwargs)
            pages.extend(result["results"])
            if not result.get("has_more"):
                break
            cursor = result["next_cursor"]
        return pages

    def get_page_title(self, page: dict) -> str:
        """Extract plain-text title from a page object."""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                rt = prop.get("title", [])
                return rt[0].get("plain_text", "Untitled") if rt else "Untitled"
        return "Untitled"

    def append_text(self, page_id: str, text: str) -> None:
        """Append a paragraph block to an existing page."""
        self.client.blocks.children.append(
            block_id=page_id,
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                },
            }],
        )

    def create_page(self, database_id: str, title: str, content: str = "") -> dict:
        """Create a new page in a database with optional body content."""
        children = []
        if content:
            for para in content.split("\n\n"):
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": para.strip()}}]
                    },
                })
        return self.client.pages.create(
            parent={"database_id": database_id},
            properties={
                "Name": {"title": [{"type": "text", "text": {"content": title}}]}
            },
            children=children,
        )
