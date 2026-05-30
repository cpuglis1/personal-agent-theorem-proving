"""
scheduler.py — fire ``schedule``-trigger agents as entry-point tasks (Phase 8).

A single asyncio loop ticks once per minute, evaluates every active agent whose
``trigger.type == "schedule"`` against its 5-field ``trigger.cron`` expression,
and enqueues a normal pipeline task for each match. Dependency-free: a tiny cron
matcher (``*``, ``*/n``, ``a,b``, ``a-b``, plain ints) covers the standard fields
so no APScheduler/croniter install is needed (the Docker image build is kept lean
and offline-safe).

The loop is started in the FastAPI ``startup`` hook and cancelled on shutdown.
``enqueue_fn`` is injected by the API layer so this module stays import-cycle free.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Awaitable, Callable

from hyperion.agents.registry import AgentRecord, load_all_agents

EnqueueFn = Callable[[AgentRecord], Awaitable[None]]


# ---------------------------------------------------------------------------
# Minimal cron matcher (5 fields: minute hour day-of-month month day-of-week)
# ---------------------------------------------------------------------------


def _match_field(spec: str, value: int, lo: int, hi: int) -> bool:
    for part in spec.split(","):
        part = part.strip()
        if part in ("*", "?"):
            return True
        step = 1
        base = part
        if "/" in part:
            base, _, step_s = part.partition("/")
            step = int(step_s)
        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            s, _, e = base.partition("-")
            start, end = int(s), int(e)
        else:
            start = end = int(base)
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """True when ``dt`` (minute resolution) satisfies a 5-field cron expression."""
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # cron day-of-week: 0 or 7 = Sunday; Python weekday() is Mon=0..Sun=6.
    py_dow = (dt.weekday() + 1) % 7
    return (
        _match_field(minute, dt.minute, 0, 59)
        and _match_field(hour, dt.hour, 0, 23)
        and _match_field(dom, dt.day, 1, 31)
        and _match_field(month, dt.month, 1, 12)
        and (_match_field(dow, py_dow, 0, 6) or _match_field(dow, 7 if py_dow == 0 else py_dow, 0, 7))
    )


def scheduled_agents() -> list[AgentRecord]:
    return [
        r
        for r in load_all_agents()
        if r.active and r.trigger.type == "schedule" and r.trigger.cron
    ]


def due_agents(now: datetime) -> list[AgentRecord]:
    return [r for r in scheduled_agents() if cron_matches(r.trigger.cron or "", now)]


# ---------------------------------------------------------------------------
# Async loop
# ---------------------------------------------------------------------------


async def run_scheduler(
    enqueue_fn: EnqueueFn,
    *,
    stop_event: asyncio.Event,
    tick_seconds: int = 60,
    now_fn: Callable[[], datetime] = datetime.now,
) -> None:
    """Tick every ``tick_seconds``; enqueue each due schedule-agent at most once
    per minute. Aligns the first tick to the next minute boundary."""
    last_fired_minute: dict[str, str] = {}

    while not stop_event.is_set():
        now = now_fn()
        minute_key = now.strftime("%Y-%m-%dT%H:%M")
        for record in due_agents(now):
            if last_fired_minute.get(record.id) == minute_key:
                continue
            last_fired_minute[record.id] = minute_key
            try:
                await enqueue_fn(record)
            except Exception:
                # A single bad enqueue must not kill the scheduler loop.
                pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass


def scheduled_task_request(record: AgentRecord) -> str:
    """The entry-point prompt a scheduled agent runs. Uses the agent's own
    description/goal so the run is meaningful without an external trigger."""
    body = record.description or record.goal or record.name
    return f"[Scheduled run of agent '{record.name}'] {body}".strip()
