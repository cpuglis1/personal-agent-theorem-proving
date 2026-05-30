"""
FastAPI service for Hyperion.

Endpoints:
  GET  /config                    → current model assignments + provider key status
  POST /tasks                     → submit a task
  GET  /tasks/{task_id}           → status + links
  GET  /tasks/{task_id}/stream    → SSE progress stream
  GET  /tasks/{task_id}/artifacts/{name} → static file
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
import zipfile
from pathlib import Path
from typing import Any, AsyncGenerator, Literal, Optional

import aiosqlite
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from hyperion.agents.registry import (
    MODEL_ALIASES,
    TOOL_REGISTRY,
    AgentRecord,
    delete_agent,
    load_agent,
    load_all_agents,
    save_agent,
    validate_agent,
    validate_collection,
)
from hyperion import scheduler, usage
from hyperion.config import settings
from hyperion.crews.plan_contract import parse_plan
from hyperion.crews.runner import resume_task, run_task
from hyperion.crews.workflows import WorkflowRecord
from hyperion.memory.episodic import store_episode
from hyperion.server.affordances import Affordance, AffordanceOption
from hyperion.server.webhooks import UnsafeCallbackURL, fire_callback, validate_callback_url

logger = logging.getLogger(__name__)

app = FastAPI(title="Hyperion", version="0.1.0")

# The Phase 7 web UI is served from a different origin (nginx :4102) and calls this
# API directly from the browser. Allow the local UI origins only — this is a
# localhost developer tool, not a public service.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

_DB_PATH = settings.tasks_dir / "state.db"
_PROGRESS: dict[str, list[str]] = {}  # task_id → list of progress lines


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


# All columns added in Phase 0 (PLAN_UNIFIED.md §4.4); later phases only populate them.
_NEW_COLUMNS = {
    "hitl": "TEXT",
    "pending_stage": "TEXT",
    "pending_payload": "TEXT",
    "resume_token": "TEXT",
    "routing": "TEXT",
    "callback_url": "TEXT",  # (Phase 9) outbound completion webhook
    "workflow": "TEXT",      # workflow id this task runs (null → server default)
}


async def _migrate(db: aiosqlite.Connection) -> None:
    """Add nullable columns to a pre-existing tasks table (CREATE IF NOT EXISTS
    will not add columns to an already-created table)."""
    async with db.execute("PRAGMA table_info(tasks)") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    for name, sqltype in _NEW_COLUMNS.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sqltype}")
    await db.commit()


async def _get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(_DB_PATH)
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
            routing TEXT,
            callback_url TEXT,
            workflow TEXT
        )"""
    )
    await db.commit()
    await _migrate(db)
    return db


async def _update_task(task_id: str, **kwargs: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            f"UPDATE tasks SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
            values,
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Progress persistence — survives an API restart so a paused task can be polled
# and resumed even after the in-memory _PROGRESS dict is gone.
# ---------------------------------------------------------------------------


def _progress_log_path(task_id: str) -> Path:
    return settings.tasks_dir / task_id / "progress.log"


def _append_progress(task_id: str, line: str) -> None:
    _PROGRESS.setdefault(task_id, []).append(line)
    try:
        path = _progress_log_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # progress logging must never break a task
        pass


def _rehydrate_progress() -> None:
    """Reload progress lines from disk so a restarted API can keep streaming."""
    if not settings.tasks_dir.exists():
        return
    for task_dir in settings.tasks_dir.iterdir():
        log = task_dir / "progress.log"
        if log.exists():
            try:
                _PROGRESS[task_dir.name] = log.read_text(encoding="utf-8").splitlines()
            except Exception:
                pass


def _plan_affordance(task_id: str) -> dict:
    """Build the choice affordance shown while a task awaits plan approval."""
    fm = parse_plan(task_id)
    options = [
        AffordanceOption(id=o.id, label=o.summary or o.id, description=o.summary or "")
        for o in fm.options
    ]
    if not options:
        options = [AffordanceOption(id="default", label="Proceed with the plan", description="")]
    return Affordance(
        type="choice",
        prompt="Review the plan and choose how to proceed.",
        options=options,
        agent_id="planner",
        stage="plan",
    ).model_dump()


def _question_affordance(task_id: str) -> Optional[dict]:
    """Build the question affordance for a task paused on an ``ask_user`` request."""
    from hyperion.feedback import latest_pending_affordance

    pending = latest_pending_affordance(task_id)
    if pending is None:
        return None
    return Affordance(
        type="question",
        prompt=pending.get("prompt", "The agent needs more information."),
        agent_id=pending.get("agent_id") or "planner",
        stage=pending.get("stage") or "plan",
    ).model_dump()


def _pending_affordance(task_id: str, status: str) -> Optional[dict]:
    """Surface the right affordance for whichever pending state a task is in."""
    if status == "awaiting_approval":
        return _plan_affordance(task_id)
    if status == "awaiting_input":
        return _question_affordance(task_id)
    return None


async def _persist_result(task_id: str, result: dict) -> None:
    """Write a runner/resume result back to the tasks table."""
    status = result["status"]
    if status in ("awaiting_approval", "awaiting_input"):
        await _update_task(
            task_id,
            status=status,
            pending_stage=result.get("pending_stage"),
            pending_payload=json.dumps(result.get("pending_payload") or {}),
            resume_token=str(uuid.uuid4())[:8],
        )
    else:
        routing = result.get("routing")
        await _update_task(
            task_id,
            status=status,
            error=result.get("error"),
            result_path=result.get("result_path"),
            routing=json.dumps(routing) if routing is not None else None,
            pending_stage=None,
            pending_payload=None,
        )


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


_MODEL_FIELDS = ("model_planner", "model_worker", "model_cheap")


def _model_override_path() -> Path:
    # Computed at call time so a patched config_dir (tests/Docker) is respected.
    return settings.config_dir / "models.json"


def _apply_model_overrides() -> None:
    """Re-apply persisted PUT /config model choices to in-memory settings on boot."""
    path = _model_override_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return
    for field in _MODEL_FIELDS:
        if data.get(field):
            setattr(settings, field, data[field])
    if data.get("default_workflow"):
        settings.default_workflow = data["default_workflow"]


# Global cap fields (token/wall/loop budgets) editable via PUT /thresholds.
_CAP_FIELDS = ("cap_input_tokens", "cap_output_tokens", "cap_tool_loop", "cap_wall_seconds")


def _thresholds_path() -> Path:
    return settings.config_dir / "thresholds.json"


def _apply_threshold_overrides() -> None:
    """Re-apply persisted PUT /thresholds global caps to settings on boot."""
    path = _thresholds_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return
    for field in _CAP_FIELDS:
        if isinstance(data.get(field), int):
            setattr(settings, field, data[field])


@app.on_event("startup")
async def _startup() -> None:
    settings.tasks_dir.mkdir(parents=True, exist_ok=True)
    # Ensure DB schema exists
    db = await _get_db()
    await db.close()
    # Reload progress for tasks that were running/paused before this restart.
    _rehydrate_progress()
    # Re-apply any persisted model assignments (PUT /config) and caps (PUT /thresholds).
    _apply_model_overrides()
    _apply_threshold_overrides()
    # Install per-agent token-cap enforcement + usage accounting (Phase 8).
    usage.register()
    # Start the schedule-trigger loop (Phase 8). Cancelled on shutdown.
    app.state.scheduler_stop = asyncio.Event()
    app.state.scheduler_task = asyncio.create_task(
        scheduler.run_scheduler(_enqueue_scheduled, stop_event=app.state.scheduler_stop)
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    stop = getattr(app.state, "scheduler_stop", None)
    task = getattr(app.state, "scheduler_task", None)
    if stop is not None:
        stop.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


async def _enqueue_scheduled(record: AgentRecord) -> None:
    """Scheduler callback: start a normal pipeline task originated by a
    schedule-trigger agent. Mirrors POST /tasks so scheduled runs are first-class."""
    task_id = str(uuid.uuid4())[:8]
    request = scheduler.scheduled_task_request(record)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO tasks (task_id, status, request, hitl) VALUES (?,?,?,?)",
            (task_id, "queued", request, "off"),
        )
        await db.commit()
    asyncio.create_task(_run_and_update(task_id, request, False, None, None, None, "off"))


# ---------------------------------------------------------------------------
# Config endpoint
# ---------------------------------------------------------------------------


@app.get("/config")
async def get_config() -> dict:
    """
    Show current model assignments and which provider API keys are configured.

    Model aliases (smart / worker / cheap) are multi-provider groups in LiteLLM —
    the proxy automatically picks from whichever providers have valid keys.
    Override any assignment by setting MODEL_PLANNER / MODEL_WORKER / MODEL_CHEAP
    in agents/hyperion/.env and restarting.
    """
    providers = settings.provider_keys_present()
    return {
        "models": {
            "planner": {
                "alias": settings.model_planner,
                "note": "high-stakes planning (Planner agent)",
                "env_var": "MODEL_PLANNER",
            },
            "worker": {
                "alias": settings.model_worker,
                "note": "research + synthesis (Researcher + Synthesizer agents)",
                "env_var": "MODEL_WORKER",
            },
            "cheap": {
                "alias": settings.model_cheap,
                "note": "summarization sub-calls (tool compression)",
                "env_var": "MODEL_CHEAP",
            },
        },
        "providers": {
            name: {"key_present": present, "status": "available" if present else "no key — alias will skip"}
            for name, present in providers.items()
        },
        "alias_fallback_order": {
            "smart":  ["claude-opus-4-6 (anthropic)", "gemini-2.5-pro (gemini)", "gpt-4o (openai)"],
            "worker": ["claude-sonnet-4-6 (anthropic)", "gemini-2.5-pro (gemini)", "gpt-4o (openai)"],
            "cheap":  ["claude-haiku-4-5 (anthropic)", "gemini-2.5-flash (gemini)", "gpt-4o-mini (openai)"],
            "fast":   ["gemini-2.5-flash (gemini)", "claude-haiku-4-5 (anthropic)", "gpt-4o-mini (openai)"],
        },
        "caps": {
            "input_tokens": settings.cap_input_tokens,
            "output_tokens": settings.cap_output_tokens,
            "tool_loop": settings.cap_tool_loop,
            "wall_seconds": settings.cap_wall_seconds,
        },
        "default_workflow": settings.default_workflow,
        "litellm_url": settings.litellm_base_url,
    }


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class TaskRequest(BaseModel):
    task: str
    schema_version: int = 1                              # (Phase 9) external-caller contract
    hitl: Literal["off", "plan", "full"] = "off"         # (filled: Phase 3)
    critic: bool = False                                 # (existing)
    workflow: Optional[str] = None                       # DAG id; null → server default
    callback_url: Optional[str] = None                   # (filled: Phase 9)
    cap_wall_seconds: Optional[int] = None
    cap_input_tokens: Optional[int] = None
    cap_output_tokens: Optional[int] = None


class TaskResponse(BaseModel):
    task_id: str
    status: str                                          # queued|running|awaiting_approval|awaiting_input|done|failed
    error: Optional[str] = None
    result_path: Optional[str] = None
    progress_lines: list[str] = []
    routing: Optional[dict] = None                       # (filled: Phase 2)
    pending_stage: Optional[str] = None                  # (filled: Phase 3)
    pending_affordance: Optional[dict] = None            # (filled: Phase 3)


class ApproveRequest(BaseModel):
    action: Literal["approve", "revise", "reject"] = "approve"
    chosen_option: Optional[str] = None                  # which plan option to run
    edits: Optional[str] = None                          # revision feedback for the planner


class FeedbackRequest(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------


def _maybe_store_episode(task_id: str, request: str, result: dict, started: float) -> None:
    """Persist a one-line episode summary so future tasks can recall similar work.
    Best-effort — failure to store should never affect the task result."""
    try:
        summary = ""
        result_path = result.get("result_path")
        if result_path and Path(result_path).exists():
            summary = Path(result_path).read_text(encoding="utf-8")[:2000]
        store_episode(
            task_id=task_id,
            original_request=request,
            final_summary=summary or (result.get("error") or "(no output)"),
            success=(result["status"] == "done"),
            duration_seconds=asyncio.get_event_loop().time() - started,
        )
    except Exception as exc:
        logger.warning("Episode store skipped for %s: %s", task_id, exc)


async def _maybe_fire_callback(task_id: str, result: dict) -> None:
    """POST the terminal result to the task's callback_url, once, if one was given.
    Looked up from the DB so it survives a pause/resume across an API restart."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT callback_url FROM tasks WHERE task_id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
    url = row["callback_url"] if row else None
    if not url:
        return
    payload = {
        "task_id": task_id,
        "status": result.get("status"),
        "error": result.get("error"),
        "result_path": result.get("result_path"),
    }
    ok = await fire_callback(url, payload)
    _append_progress(task_id, f"[hyperion] callback {'delivered' if ok else 'failed'} → {url}")


async def _run_and_update(
    task_id: str,
    request: str,
    critic: bool,
    cap_wall_seconds: Optional[int],
    cap_input_tokens: Optional[int],
    cap_output_tokens: Optional[int],
    hitl: str = "off",
    workflow: Optional[str] = None,
) -> None:
    _PROGRESS[task_id] = []
    started = asyncio.get_event_loop().time()

    def _progress(line: str) -> None:
        _append_progress(task_id, line)

    await _update_task(task_id, status="running")
    result = await run_task(
        task_id=task_id,
        request=request,
        progress_callback=_progress,
        cap_wall_seconds=cap_wall_seconds,
        cap_input_tokens=cap_input_tokens,
        cap_output_tokens=cap_output_tokens,
        hitl=hitl,
        workflow=workflow,
    )
    await _persist_result(task_id, result)
    _progress(f"[hyperion] status={result['status']}")

    if result["status"] in ("awaiting_approval", "awaiting_input"):
        return  # paused for human input; episode stored after the task finishes
    _maybe_store_episode(task_id, request, result, started)
    await _maybe_fire_callback(task_id, result)


async def _resume_and_update(
    task_id: str,
    request: str,
    action: str,
    chosen_option: Optional[str],
    edits: Optional[str],
    payload: dict,
) -> None:
    started = asyncio.get_event_loop().time()

    def _progress(line: str) -> None:
        _append_progress(task_id, line)

    caps = payload.get("caps") or {}
    result = await resume_task(
        task_id=task_id,
        request=request,
        action=action,
        chosen_option=chosen_option,
        edits=edits,
        hitl=payload.get("hitl", "off"),
        revise_count=payload.get("revise_count", 0),
        workflow=payload.get("workflow"),
        resume_node=payload.get("resume_node"),
        progress_callback=_progress,
        cap_wall_seconds=caps.get("cap_wall_seconds"),
        cap_input_tokens=caps.get("cap_input_tokens"),
        cap_output_tokens=caps.get("cap_output_tokens"),
    )
    await _persist_result(task_id, result)
    _progress(f"[hyperion] status={result['status']}")

    if result["status"] in ("awaiting_approval", "awaiting_input"):
        return  # re-paused after a revision / new question
    _maybe_store_episode(task_id, request, result, started)
    await _maybe_fire_callback(task_id, result)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/tasks", response_model=TaskResponse, status_code=202)
async def submit_task(body: TaskRequest) -> TaskResponse:
    if body.schema_version != 1:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported schema_version {body.schema_version} (this server speaks v1)",
        )
    if body.callback_url:
        try:
            validate_callback_url(body.callback_url)
        except UnsafeCallbackURL as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if body.workflow:
        from hyperion.crews.workflows import load_workflow

        try:
            load_workflow(body.workflow)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Unknown workflow: {exc}")

    task_id = str(uuid.uuid4())[:8]
    db = await _get_db()  # creates + migrates the table (adds callback_url if absent)
    try:
        await db.execute(
            "INSERT INTO tasks (task_id, status, request, hitl, callback_url, workflow) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, "queued", body.task, body.hitl, body.callback_url, body.workflow),
        )
        await db.commit()
    finally:
        await db.close()

    asyncio.create_task(
        _run_and_update(
            task_id,
            body.task,
            body.critic,
            body.cap_wall_seconds,
            body.cap_input_tokens,
            body.cap_output_tokens,
            body.hitl,
            body.workflow,
        )
    )
    return TaskResponse(task_id=task_id, status="queued")


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str) -> TaskResponse:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT task_id, status, error, result_path, pending_stage, routing "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    routing = json.loads(row["routing"]) if row["routing"] else None
    affordance = _pending_affordance(task_id, row["status"])
    return TaskResponse(
        task_id=row["task_id"],
        status=row["status"],
        error=row["error"],
        result_path=row["result_path"],
        progress_lines=_PROGRESS.get(task_id, []),
        routing=routing,
        pending_stage=row["pending_stage"],
        pending_affordance=affordance,
    )


@app.post("/tasks/{task_id}/approve", response_model=TaskResponse)
async def approve_task(task_id: str, body: ApproveRequest) -> TaskResponse:
    """Resume a task paused at the plan gate.

    Reads the pending payload from the DB (not in-memory) so a restarted API can
    still resume. action ∈ {approve, revise, reject}.
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT status, request, pending_payload FROM tasks WHERE task_id=?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    if row["status"] != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Task is not awaiting approval (status={row['status']})",
        )
    payload = json.loads(row["pending_payload"]) if row["pending_payload"] else {}

    await _update_task(task_id, status="running")
    asyncio.create_task(
        _resume_and_update(
            task_id, row["request"], body.action, body.chosen_option, body.edits, payload
        )
    )
    return TaskResponse(task_id=task_id, status="running")


@app.post("/tasks/{task_id}/feedback", response_model=TaskResponse)
async def feedback_task(task_id: str, body: FeedbackRequest) -> TaskResponse:
    """Push free-text human feedback to a task.

    For a *running* task the runner drains it between stages (changes behavior). For a
    task paused on an ``ask_user`` question (status=awaiting_input) the message answers
    the affordance and resumes the plan stage with the answer injected.
    """
    from hyperion.feedback import answer_affordance, append_feedback

    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT status, request, pending_payload FROM tasks WHERE task_id=?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    if row["status"] == "awaiting_input":
        # The message answers the planner's question; resume by re-running the plan
        # stage with the answer fed back as revision feedback.
        answer_affordance(task_id, body.message)
        payload = json.loads(row["pending_payload"]) if row["pending_payload"] else {}
        await _update_task(task_id, status="running")
        asyncio.create_task(
            _resume_and_update(task_id, row["request"], "revise", None, body.message, payload)
        )
        return TaskResponse(task_id=task_id, status="running")

    # Running (or any other) task: queue the feedback for the next stage drain.
    append_feedback(task_id, body.message)
    return TaskResponse(task_id=task_id, status=row["status"])


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str) -> StreamingResponse:
    async def _gen() -> AsyncGenerator[str, None]:
        seen = 0
        while True:
            lines = _PROGRESS.get(task_id, [])
            for line in lines[seen:]:
                yield f"data: {line}\n\n"
                seen += 1
            # Check if task is done
            async with aiosqlite.connect(_DB_PATH) as db:
                async with db.execute(
                    "SELECT status FROM tasks WHERE task_id=?", (task_id,)
                ) as cur:
                    row = await cur.fetchone()
            if row and row[0] in ("done", "failed"):
                yield f"data: [DONE] status={row[0]}\n\n"
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.get("/tasks/{task_id}/artifacts/{name:path}")
async def get_artifact(task_id: str, name: str) -> FileResponse:
    # Validate task_id shape to prevent traversal via the path param itself
    if not task_id or "/" in task_id or ".." in task_id:
        raise HTTPException(status_code=400, detail="Invalid task_id")
    base = (settings.tasks_dir / task_id).resolve()
    artifact_path = (base / "artifacts" / name).resolve()
    # Use is_relative_to (Python 3.9+) — startswith allows sibling-dir attacks
    if not artifact_path.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(artifact_path)


# ---------------------------------------------------------------------------
# Agent CRUD + options API (Phase 5) — additive endpoints, no model restructuring.
# ---------------------------------------------------------------------------


async def _litellm_model_ids() -> list[str]:
    """Concrete model ids the proxy reports. Best-effort — empty on any failure."""
    import httpx

    url = settings.litellm_base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {settings.llm_api_key}"})
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])]
    except Exception as exc:
        logger.warning("Could not fetch LiteLLM models: %s", exc)
        return []


async def _assert_model_alias_valid(record: AgentRecord) -> None:
    """Reject an unknown model_alias — but only when we can actually see the model
    list (offline edits aren't blocked by a transient proxy outage)."""
    if record.model_alias in MODEL_ALIASES:
        return
    known = await _litellm_model_ids()
    if known and record.model_alias not in known:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown model_alias {record.model_alias!r}. "
                   f"Use one of {MODEL_ALIASES} or a concrete model id.",
        )


def _validate_mutation(records: list[AgentRecord]) -> None:
    """Run whole-store invariants, surfacing failures as 422s."""
    try:
        validate_collection(records)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/agents")
async def list_agents(group: Optional[str] = None) -> list[dict]:
    records = load_all_agents()
    if group:
        records = [r for r in records if r.group == group]
    return [r.model_dump() for r in records]


@app.get("/groups")
async def list_groups() -> list[str]:
    """Distinct agent groups, for the UI group filter."""
    return sorted({r.group for r in load_all_agents()})


@app.get("/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    try:
        return load_agent(agent_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No agent {agent_id!r}")


@app.post("/agents", status_code=201)
async def create_agent(record: AgentRecord) -> dict:
    existing = {r.id: r for r in load_all_agents()}
    if record.id in existing:
        raise HTTPException(status_code=409, detail=f"Agent {record.id!r} already exists")
    try:
        validate_agent(record)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await _assert_model_alias_valid(record)
    _validate_mutation(list(existing.values()) + [record])
    save_agent(record)
    return record.model_dump()


@app.put("/agents/{agent_id}")
async def update_agent(agent_id: str, record: AgentRecord) -> dict:
    if record.id != agent_id:
        raise HTTPException(status_code=422, detail="Body id must match the URL id")
    existing = {r.id: r for r in load_all_agents()}
    if agent_id not in existing:
        raise HTTPException(status_code=404, detail=f"No agent {agent_id!r}")
    try:
        validate_agent(record)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await _assert_model_alias_valid(record)
    existing[agent_id] = record  # apply the edit to the prospective set
    _validate_mutation(list(existing.values()))
    save_agent(record)
    return record.model_dump()


@app.delete("/agents/{agent_id}")
async def remove_agent(agent_id: str) -> dict:
    existing = {r.id: r for r in load_all_agents()}
    if agent_id not in existing:
        raise HTTPException(status_code=404, detail=f"No agent {agent_id!r}")
    del existing[agent_id]
    _validate_mutation(list(existing.values()))  # blocks deleting the last plan/synth
    delete_agent(agent_id)
    return {"deleted": agent_id}


@app.post("/agents/{agent_id}/duplicate", status_code=201)
async def duplicate_agent(agent_id: str, new_id: Optional[str] = None) -> dict:
    existing = {r.id: r for r in load_all_agents()}
    if agent_id not in existing:
        raise HTTPException(status_code=404, detail=f"No agent {agent_id!r}")
    src = existing[agent_id]
    target = new_id or f"{agent_id}-copy"
    if target in existing:
        raise HTTPException(status_code=409, detail=f"Agent {target!r} already exists")
    clone = src.model_copy(update={"id": target, "name": f"{src.name} (copy)"})
    try:
        validate_agent(clone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    save_agent(clone)
    return clone.model_dump()


# ---------------------------------------------------------------------------
# Workflow CRUD — named DAGs of agent nodes, picked per-run or as the default.
# ---------------------------------------------------------------------------


def _validate_workflow_record(record) -> None:
    """Structural + agent-reference validation, surfaced as a 422."""
    from hyperion.crews.workflows import validate_workflow

    known = {r.id for r in load_all_agents()}
    try:
        validate_workflow(record, known)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/workflows")
async def list_workflows() -> list[dict]:
    from hyperion.crews.workflows import load_all_workflows

    return [w.model_dump() for w in load_all_workflows()]


@app.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str) -> dict:
    from hyperion.crews.workflows import load_workflow

    try:
        return load_workflow(workflow_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No workflow {workflow_id!r}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/workflows", status_code=201)
async def create_workflow(record: WorkflowRecord) -> dict:
    from hyperion.crews.workflows import load_all_workflows, save_workflow

    existing = {w.id for w in load_all_workflows()}
    if record.id in existing:
        raise HTTPException(status_code=409, detail=f"Workflow {record.id!r} already exists")
    _validate_workflow_record(record)
    save_workflow(record)
    return record.model_dump()


@app.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, record: WorkflowRecord) -> dict:
    from hyperion.crews.workflows import load_all_workflows, save_workflow

    if record.id != workflow_id:
        raise HTTPException(status_code=422, detail="Body id must match the URL id")
    if workflow_id not in {w.id for w in load_all_workflows()}:
        raise HTTPException(status_code=404, detail=f"No workflow {workflow_id!r}")
    _validate_workflow_record(record)
    save_workflow(record)
    return record.model_dump()


@app.delete("/workflows/{workflow_id}")
async def remove_workflow(workflow_id: str) -> dict:
    from hyperion.crews.workflows import delete_workflow, load_all_workflows

    existing = {w.id for w in load_all_workflows()}
    if workflow_id not in existing:
        raise HTTPException(status_code=404, detail=f"No workflow {workflow_id!r}")
    if len(existing) == 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last workflow")
    if workflow_id == settings.default_workflow:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the default workflow; set a different default first",
        )
    delete_workflow(workflow_id)
    return {"deleted": workflow_id}


@app.post("/workflows/{workflow_id}/duplicate", status_code=201)
async def duplicate_workflow(workflow_id: str, new_id: Optional[str] = None) -> dict:
    from hyperion.crews.workflows import load_all_workflows, load_workflow, save_workflow

    existing = {w.id for w in load_all_workflows()}
    if workflow_id not in existing:
        raise HTTPException(status_code=404, detail=f"No workflow {workflow_id!r}")
    target = new_id or f"{workflow_id}-copy"
    if target in existing:
        raise HTTPException(status_code=409, detail=f"Workflow {target!r} already exists")
    src = load_workflow(workflow_id)
    clone = src.model_copy(update={"id": target, "name": f"{src.name} (copy)"})
    _validate_workflow_record(clone)
    save_workflow(clone)
    return clone.model_dump()


@app.get("/tools")
async def list_tools() -> list[dict]:
    """Tool names available to agents, with a description sampled from each factory."""
    out = []
    for name in sorted(TOOL_REGISTRY):
        desc = ""
        try:
            desc = getattr(TOOL_REGISTRY[name]("_probe"), "description", "")
        except Exception:
            pass
        out.append({"name": name, "description": desc})
    return out


@app.get("/models")
async def list_models() -> dict:
    """Role aliases plus the concrete models the proxy currently exposes."""
    return {
        "aliases": list(MODEL_ALIASES),
        "models": await _litellm_model_ids(),
        "current": {
            "planner": settings.model_planner,
            "worker": settings.model_worker,
            "cheap": settings.model_cheap,
        },
    }


class ConfigUpdate(BaseModel):
    model_planner: Optional[str] = None
    model_worker: Optional[str] = None
    model_cheap: Optional[str] = None
    default_workflow: Optional[str] = None


@app.put("/config")
async def update_config(body: ConfigUpdate) -> dict:
    """Reassign role models / default workflow live (no restart) and persist for boot."""
    known = await _litellm_model_ids()
    updates: dict[str, str] = {}
    for field in _MODEL_FIELDS:
        value = getattr(body, field)
        if not value:
            continue
        if value not in MODEL_ALIASES and known and value not in known:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown model {value!r} for {field}. "
                       f"Use one of {MODEL_ALIASES} or a concrete model id.",
            )
        updates[field] = value

    if body.default_workflow:
        from hyperion.crews.workflows import load_workflow

        try:
            load_workflow(body.default_workflow)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Unknown workflow: {exc}")
        updates["default_workflow"] = body.default_workflow

    for field, value in updates.items():
        setattr(settings, field, value)  # live effect

    if updates:
        path = _model_override_path()
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    return await get_config()


# ---------------------------------------------------------------------------
# Phase 8 — thresholds, paginated tasks, metrics, monitoring deep-links
# ---------------------------------------------------------------------------


def _langfuse_session_url(task_id: str) -> Optional[str]:
    """Deep-link to the Langfuse session whose id == task_id (set in llms.py).
    Fully resolvable only when langfuse_project_id is configured."""
    host = (settings.langfuse_public_url or settings.langfuse_host or "").rstrip("/")
    if not host:
        return None
    if settings.langfuse_project_id:
        return f"{host}/project/{settings.langfuse_project_id}/sessions/{task_id}"
    return f"{host}/sessions/{task_id}"


class ThresholdUpdate(BaseModel):
    cap_input_tokens: Optional[int] = None
    cap_output_tokens: Optional[int] = None
    cap_tool_loop: Optional[int] = None
    cap_wall_seconds: Optional[int] = None
    # Per-agent token/activation overrides: {agent_id: {max_input_tokens: int, ...}}
    agents: Optional[dict[str, dict[str, Optional[int]]]] = None


@app.get("/thresholds")
async def get_thresholds() -> dict:
    """Global caps plus each agent's per-record token/activation thresholds."""
    agents = {
        r.id: r.thresholds.model_dump()
        for r in load_all_agents()
    }
    return {
        "global": {field: getattr(settings, field) for field in _CAP_FIELDS},
        "agents": agents,
    }


@app.put("/thresholds")
async def update_thresholds(body: ThresholdUpdate) -> dict:
    """Update global caps (live + persisted) and/or per-agent thresholds (written
    back into the agent records). Additive — leaves unspecified fields untouched."""
    updates: dict[str, int] = {}
    for field in _CAP_FIELDS:
        value = getattr(body, field)
        if value is None:
            continue
        if not isinstance(value, int) or value <= 0:
            raise HTTPException(status_code=422, detail=f"{field} must be a positive integer")
        updates[field] = value

    for field, value in updates.items():
        setattr(settings, field, value)  # live effect

    if updates:
        path = _thresholds_path()
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    # Per-agent threshold overrides are written into the agent JSON records.
    for agent_id, fields in (body.agents or {}).items():
        record = load_agent(agent_id)  # 404s naturally if missing
        th = record.thresholds.model_dump()
        for key in ("max_input_tokens", "max_output_tokens", "max_activations_per_day"):
            if key in fields:
                th[key] = fields[key]
        record.thresholds = type(record.thresholds)(**th)
        save_agent(record)

    return await get_thresholds()


def _iso_utc(ts: Optional[str]) -> Optional[str]:
    """SQLite CURRENT_TIMESTAMP is naive UTC ('YYYY-MM-DD HH:MM:SS'). Tag it as
    ISO-8601 UTC ('...TZ') so the browser renders it in the viewer's local
    timezone instead of mis-parsing the naive string as local time."""
    if not ts or "T" in ts or ts.endswith("Z") or "+" in ts:
        return ts
    return ts.replace(" ", "T", 1) + "Z"


@app.get("/tasks")
async def list_tasks(limit: int = 50, offset: int = 0) -> dict:
    """Paginated run history, newest first, for the monitoring page."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) AS n FROM tasks") as cur:
            total = (await cur.fetchone())["n"]
        async with db.execute(
            "SELECT task_id, status, request, error, created_at, updated_at, hitl "
            "FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
    items = []
    for r in rows:
        items.append(
            {
                "task_id": r["task_id"],
                "status": r["status"],
                "request": (r["request"] or "")[:200],
                "error": r["error"],
                "created_at": _iso_utc(r["created_at"]),
                "updated_at": _iso_utc(r["updated_at"]),
                "hitl": r["hitl"],
                "langfuse_url": _langfuse_session_url(r["task_id"]),
            }
        )
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/metrics")
async def get_metrics() -> dict:
    """Per-agent activation counts + error rate (from the routing column) and live
    token usage (from the in-process usage accountant). Powers the monitoring tiles."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status, routing FROM tasks") as cur:
            rows = await cur.fetchall()

    status_counts: dict[str, int] = {}
    per_agent: dict[str, dict[str, int]] = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        if not r["routing"]:
            continue
        try:
            routing = json.loads(r["routing"])
        except (json.JSONDecodeError, TypeError):
            continue
        # Routing is persisted as RoutingResult: {"selected_agents": [id, ...]}.
        selected = routing.get("selected_agents") or routing.get("selected") or []
        for a in selected:
            aid = a.get("id") if isinstance(a, dict) else a
            if not aid:
                continue
            bucket = per_agent.setdefault(aid, {"activations": 0, "errors": 0})
            bucket["activations"] += 1
            if r["status"] == "failed":
                bucket["errors"] += 1

    tokens = usage.all_agent_totals()
    caps = {field: getattr(settings, field) for field in _CAP_FIELDS}
    agents = []
    for record in load_all_agents():
        stats = per_agent.get(record.id, {"activations": 0, "errors": 0})
        acts = stats["activations"]
        errs = stats["errors"]
        agents.append(
            {
                "id": record.id,
                "name": record.name,
                "stage": record.stage,
                "active": record.active,
                "activations": acts,
                "errors": errs,
                "error_rate": round(errs / acts, 3) if acts else 0.0,
                "tokens": tokens.get(record.id, {"input": 0, "output": 0}),
                "thresholds": record.thresholds.model_dump(),
            }
        )
    return {
        "tasks_total": sum(status_counts.values()),
        "by_status": status_counts,
        "caps": caps,
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# Phase 9 — orchestration & polish: agent card, follow-ups, config export/import
# ---------------------------------------------------------------------------


@app.get("/.well-known/agent.json")
async def agent_card() -> dict:
    """A2A-style agent descriptor so external orchestrators (n8n, other agents)
    can discover Hyperion's contract. Skills are the synthesize-stage outputs;
    the input contract is POST /tasks with schema_version:1."""
    agents = load_all_agents()
    skills = [
        {"id": r.id, "name": r.name, "description": r.description or r.goal}
        for r in agents
        if r.active and r.stage in ("plan", "synthesize")
    ]
    return {
        "schema_version": 1,
        "name": "Hyperion",
        "description": "Local multi-agent orchestrator (plan → route → work → synthesize).",
        "url": settings.hyperion_api_url,
        "version": app.version,
        "capabilities": {"streaming": True, "humanInTheLoop": True, "webhooks": True},
        "endpoints": {
            "submit": "POST /tasks",
            "status": "GET /tasks/{task_id}",
            "stream": "GET /tasks/{task_id}/stream",
            "approve": "POST /tasks/{task_id}/approve",
            "feedback": "POST /tasks/{task_id}/feedback",
        },
        "input_modes": ["text"],
        "output_modes": ["text", "file"],
        "skills": skills,
    }


def _result_text(task_id: str, result_path: Optional[str]) -> str:
    """Read a finished task's primary artifact for the save-to-notion follow-up."""
    if result_path and Path(result_path).exists():
        return Path(result_path).read_text(encoding="utf-8")
    return ""


class SaveToNotionRequest(BaseModel):
    title: Optional[str] = None


@app.post("/tasks/{task_id}/save-to-notion")
async def save_task_to_notion(task_id: str, body: SaveToNotionRequest) -> dict:
    """Synthesizer follow-up affordance: write a completed task's result to Notion."""
    from hyperion.tools.notion import create_notion_page

    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT status, request, result_path FROM tasks WHERE task_id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    if row["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Task is {row['status']}, not done")

    text = _result_text(task_id, row["result_path"])
    if not text:
        raise HTTPException(status_code=409, detail="Task has no readable result artifact")

    title = body.title or f"Hyperion: {(row['request'] or task_id)[:80]}"
    result = create_notion_page(title, text)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@app.get("/config/export")
async def export_config() -> Response:
    """Download the agent + workflow store as a zip — back up or move config."""
    from hyperion.agents.registry import _agents_dir
    from hyperion.crews.workflows import _workflows_dir

    buf = io.BytesIO()
    agents_dir = _agents_dir()
    workflows_dir = _workflows_dir()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if agents_dir.exists():
            for path in sorted(agents_dir.glob("*.json")):
                zf.write(path, arcname=f"agents/{path.name}")
        if workflows_dir.exists():
            for path in sorted(workflows_dir.glob("*.json")):
                zf.write(path, arcname=f"workflows/{path.name}")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="hyperion-config.zip"'},
    )


@app.post("/config/import")
async def import_config(file: UploadFile = File(...)) -> dict:
    """Restore an agent + workflow store from an exported zip. Validates every record
    before writing and rejects the whole import if any record is malformed (atomic).
    Entries are routed by path prefix: ``agents/*.json`` and ``workflows/*.json``;
    a flat ``*.json`` (legacy export) is treated as an agent record."""
    from hyperion.crews.workflows import save_workflow, validate_workflow

    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="Uploaded file is not a valid zip")

    records: list[AgentRecord] = []
    workflows: list[WorkflowRecord] = []
    for name in zf.namelist():
        if not name.endswith(".json") or name.endswith("/"):
            continue
        text = zf.read(name).decode("utf-8")
        if name.startswith("workflows/"):
            try:
                workflows.append(WorkflowRecord.model_validate_json(text))
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"Invalid workflow {name!r}: {exc}")
            continue
        try:
            record = AgentRecord.model_validate_json(text)
            validate_agent(record)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid record {name!r}: {exc}")
        records.append(record)

    if not records:
        raise HTTPException(status_code=422, detail="No agent records found in archive")
    _validate_mutation(records)  # enforce DAG + at-least-one-plan/synthesize on the new set

    # Validate workflows against the imported agent set (cross-reference must resolve).
    known_agents = {r.id for r in records}
    for wf in workflows:
        try:
            validate_workflow(wf, known_agents)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid workflow {wf.id!r}: {exc}")

    for record in records:
        save_agent(record)
    for wf in workflows:
        save_workflow(wf)
    return {
        "imported": [r.id for r in records],
        "count": len(records),
        "workflows": [w.id for w in workflows],
    }


def main() -> None:
    import uvicorn

    uvicorn.run("hyperion.server.api:app", host="0.0.0.0", port=4100, reload=False, log_level="info")
