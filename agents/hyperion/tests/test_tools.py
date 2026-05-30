"""
Unit tests for Hyperion tools.

Run: uv run pytest tests/test_tools.py -v
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# workspace.py
# ---------------------------------------------------------------------------


def test_workspace_write_read(tmp_path):
    from hyperion.config import settings
    from hyperion.tools.workspace import WorkspaceReadTool, WorkspaceWriteTool

    with patch.object(settings, "tasks_dir", tmp_path):
        writer = WorkspaceWriteTool(task_id="t1")
        reader = WorkspaceReadTool(task_id="t1")
        msg = writer._run("notes/test.md", "hello world")
        assert "hello world" in reader._run("notes/test.md")


def test_workspace_traversal_rejected(tmp_path):
    from hyperion.config import settings
    from hyperion.tools.workspace import WorkspaceWriteTool

    with patch.object(settings, "tasks_dir", tmp_path):
        writer = WorkspaceWriteTool(task_id="t1")
        with pytest.raises(ValueError, match="traversal"):
            writer._run("../../etc/passwd", "bad")


# ---------------------------------------------------------------------------
# reranker.py (offline — mock httpx)
# ---------------------------------------------------------------------------


def test_reranker_returns_sorted():
    from hyperion.tools.reranker import rerank

    fake_response = {
        "results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.5},
            {"index": 1, "relevance_score": 0.7},
        ]
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = fake_response
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_resp):
        ranked = rerank("query", ["a", "b", "c"], top_n=3)

    assert ranked[0][0] == 2  # highest score first
    assert ranked[0][1] == pytest.approx(0.9)


def test_reranker_graceful_fallback():
    from hyperion.tools.reranker import rerank

    with patch("httpx.post", side_effect=Exception("network down")):
        ranked = rerank("query", ["a", "b", "c"])

    # Returns original order with 0 scores
    assert [r[0] for r in ranked] == [0, 1, 2]


# ---------------------------------------------------------------------------
# web_search.py (offline — mock httpx)
# ---------------------------------------------------------------------------


def test_web_search_strips_html():
    from hyperion.tools.web_search import _strip_html

    assert _strip_html("<b>hello</b> &amp; <i>world</i>") == "hello & world"


def test_web_search_returns_untrusted_prefix():
    from hyperion.tools.web_search import WebSearchTool

    fake = {
        "results": [
            {"title": "Test", "content": "result content", "url": "http://example.com"}
        ]
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = fake
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_resp):
        with patch("hyperion.tools.web_search.rerank", return_value=[(0, 1.0)]):
            tool = WebSearchTool()
            result = tool._run("test query")

    assert "untrusted" in result.lower()
    assert "example.com" in result


# ---------------------------------------------------------------------------
# crews/default.py — CapExceeded
# ---------------------------------------------------------------------------


def test_tool_call_tracker_detects_loop():
    from hyperion.crews.default import CapExceeded, ToolCallTracker

    tracker = ToolCallTracker(cap=3)
    tracker.check("web_search", {"q": "same"})
    tracker.check("web_search", {"q": "same"})
    with pytest.raises(CapExceeded):
        tracker.check("web_search", {"q": "same"})


def test_tool_call_tracker_resets_on_different_call():
    from hyperion.crews.default import ToolCallTracker

    tracker = ToolCallTracker(cap=3)
    tracker.check("web_search", {"q": "same"})
    tracker.check("web_search", {"q": "same"})
    tracker.check("web_search", {"q": "different"})  # resets
    tracker.check("web_search", {"q": "different"})
    # No exception — count reset
