"""
Hyperion MCP server — exposes three tools to Claude Code.

Transport: streamable HTTP at http://localhost:4101/mcp

Tools:
  hyperion_run(task)          → task_id (streams notifications/progress)
  hyperion_status(task_id)    → status dict
  hyperion_artifact(task_id, name) → file contents as text
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import AsyncIterator

import aiosqlite
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from hyperion.config import settings
from hyperion.crews.default import run_crew

logger = logging.getLogger(__name__)

server = Server("hyperion")

_PROGRESS: dict[str, list[str]] = {}
_DB_PATH = settings.tasks_dir / "state.db"


async def _ensure_db() -> None:
    settings.tasks_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                status  TEXT NOT NULL DEFAULT 'queued',
                request TEXT,
                error   TEXT,
                result_path TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                hitl TEXT,
                pending_stage TEXT,
                pending_payload TEXT,
                resume_token TEXT,
                routing TEXT
            )"""
        )
        await db.commit()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="hyperion_run",
            description=(
                "Submit a task to Hyperion (CrewAI multi-agent system). "
                "The crew plans, researches, and synthesizes a result. "
                "Returns a task_id immediately; use hyperion_status to poll."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Natural-language task description"},
                    "critic": {"type": "boolean", "default": False},
                    "workflow": {
                        "type": "string",
                        "description": (
                            "Optional workflow (DAG) id to run. Omit to use the server "
                            "default. List ids via the /workflows API or the UI."
                        ),
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="hyperion_status",
            description="Get the status and result of a Hyperion task.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="hyperion_artifact",
            description="Retrieve the content of an artifact file from a completed Hyperion task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "name": {"type": "string", "default": "result.md"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="hyperion_approve",
            description=(
                "Resume a task paused at the plan-approval gate. action ∈ "
                "{approve, revise, reject}. For 'approve' you may pass chosen_option "
                "(a plan option id); for 'revise' pass edits (feedback for the planner)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["approve", "revise", "reject"],
                        "default": "approve",
                    },
                    "chosen_option": {"type": "string"},
                    "edits": {"type": "string"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="hyperion_feedback",
            description=(
                "Send free-text feedback to a Hyperion task. A running task drains it "
                "between stages; a task paused on a question (awaiting_input) treats the "
                "message as the answer and resumes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["task_id", "message"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    await _ensure_db()

    if name == "hyperion_run":
        task_id = str(uuid.uuid4())[:8]
        request = arguments["task"]
        workflow = arguments.get("workflow")

        async with aiosqlite.connect(_DB_PATH) as db:
            await db.execute(
                "INSERT INTO tasks (task_id, status, request) VALUES (?,?,?)",
                (task_id, "queued", request),
            )
            await db.commit()

        _PROGRESS[task_id] = []

        def _progress(line: str) -> None:
            _PROGRESS.setdefault(task_id, []).append(line)

        async def _run():
            async with aiosqlite.connect(_DB_PATH) as db:
                await db.execute(
                    "UPDATE tasks SET status='running', updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                    (task_id,),
                )
                await db.commit()
            result = await run_crew(
                task_id=task_id, request=request, progress_callback=_progress,
                workflow=workflow,
            )
            async with aiosqlite.connect(_DB_PATH) as db:
                await db.execute(
                    "UPDATE tasks SET status=?, error=?, result_path=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                    (result["status"], result.get("error"), result.get("result_path"), task_id),
                )
                await db.commit()

        asyncio.create_task(_run())
        return [TextContent(type="text", text=f"Task submitted. task_id={task_id}")]

    elif name == "hyperion_status":
        task_id = arguments["task_id"]
        async with aiosqlite.connect(_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT task_id, status, error, result_path FROM tasks WHERE task_id=?",
                (task_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return [TextContent(type="text", text=f"Task {task_id!r} not found.")]
        progress = _PROGRESS.get(task_id, [])
        text = (
            f"task_id: {row['task_id']}\n"
            f"status:  {row['status']}\n"
            f"error:   {row['error'] or 'none'}\n"
            f"result:  {row['result_path'] or 'pending'}\n"
            f"progress ({len(progress)} lines):\n"
            + "\n".join(f"  {l}" for l in progress[-20:])
        )
        return [TextContent(type="text", text=text)]

    elif name == "hyperion_artifact":
        task_id = arguments["task_id"]
        artifact_name = arguments.get("name", "result.md")
        path = (settings.tasks_dir / task_id / "artifacts" / artifact_name).resolve()
        base = (settings.tasks_dir / task_id).resolve()
        if not path.is_relative_to(base):
            return [TextContent(type="text", text="Error: invalid path.")]
        if not path.exists():
            return [TextContent(type="text", text=f"Artifact '{artifact_name}' not found for task {task_id}.")]
        return [TextContent(type="text", text=path.read_text(encoding="utf-8"))]

    elif name == "hyperion_approve":
        task_id = arguments["task_id"]
        payload = {
            "action": arguments.get("action", "approve"),
            "chosen_option": arguments.get("chosen_option"),
            "edits": arguments.get("edits"),
        }
        return await _post_api(f"/tasks/{task_id}/approve", payload)

    elif name == "hyperion_feedback":
        task_id = arguments["task_id"]
        return await _post_api(
            f"/tasks/{task_id}/feedback", {"message": arguments["message"]}
        )

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _post_api(path: str, payload: dict) -> list[TextContent]:
    """POST to the Hyperion FastAPI service, which owns task orchestration."""
    import httpx

    url = settings.hyperion_api_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            return [TextContent(type="text", text=f"Error {resp.status_code}: {resp.text}")]
        data = resp.json()
        return [TextContent(
            type="text",
            text=f"task_id: {data.get('task_id')}\nstatus:  {data.get('status')}",
        )]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error contacting Hyperion API: {exc}")]


class _MCPApp:
    """Thin ASGI wrapper around StreamableHTTPSessionManager.handle_request."""

    def __init__(self, sm: StreamableHTTPSessionManager) -> None:
        self._sm = sm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._sm.handle_request(scope, receive, send)


def make_starlette_app() -> Starlette:
    session_manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,  # no persistent session state needed between tool calls
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[Route("/mcp", endpoint=_MCPApp(session_manager))],
        lifespan=lifespan,
    )


def main() -> None:
    import uvicorn

    starlette_app = make_starlette_app()
    uvicorn.run(starlette_app, host="0.0.0.0", port=4101, log_level="info")


if __name__ == "__main__":
    main()
