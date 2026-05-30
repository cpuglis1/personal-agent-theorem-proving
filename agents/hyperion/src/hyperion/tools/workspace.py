"""Workspace tool — per-task scratchpad confined to tasks/{task_id}/."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from crewai.tools import BaseTool
from pydantic import Field

from hyperion.config import settings


def _safe_path(task_id: str, rel_path: str) -> Path:
    """Resolve rel_path inside the task workspace; reject traversal attempts.

    Uses Path.is_relative_to (Python 3.9+) which correctly handles sibling-dir
    escapes — a string startswith check would let "abc-evil" match "abc".
    """
    if not task_id or "/" in task_id or ".." in task_id:
        raise ValueError(f"Invalid task_id: {task_id!r}")
    base = (settings.tasks_dir / task_id).resolve()
    full = (base / rel_path).resolve()
    if not full.is_relative_to(base):
        raise ValueError(f"Path traversal rejected: {rel_path!r}")
    return full


class WorkspaceReadTool(BaseTool):
    name: str = "workspace_read"
    description: str = (
        "Read a file from the current task's workspace. "
        "Input: relative file path within the task workspace."
    )
    task_id: str = Field(...)

    def _run(self, path: str) -> str:
        target = _safe_path(self.task_id, path)
        if not target.exists():
            return f"(File not found: {path})"
        return target.read_text(encoding="utf-8")


class WorkspaceWriteTool(BaseTool):
    name: str = "workspace_write"
    description: str = (
        "Write content to a file in the current task's workspace. "
        "Input: JSON with keys 'path' (relative file path) and 'content' (string to write)."
    )
    task_id: str = Field(...)

    def _run(self, path: str, content: str = "") -> str:
        target = _safe_path(self.task_id, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {path}"


class WorkspaceListTool(BaseTool):
    name: str = "workspace_list"
    description: str = "List all files in the current task's workspace. No input needed."
    task_id: str = Field(...)

    def _run(self, _: str = "") -> str:
        base = settings.tasks_dir / self.task_id
        if not base.exists():
            return "(Workspace is empty.)"
        files = sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())
        return "\n".join(files) if files else "(Workspace is empty.)"


def workspace_tools(task_id: str) -> list:
    return [
        WorkspaceReadTool(task_id=task_id),
        WorkspaceWriteTool(task_id=task_id),
        WorkspaceListTool(task_id=task_id),
    ]
