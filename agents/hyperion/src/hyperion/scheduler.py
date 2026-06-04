"""
scheduler.py — fire scheduled agents as entry-point tasks (Phase 8).

A single asyncio loop ticks once per minute, evaluates every active agent that
carries a 5-field ``schedule_cron`` expression against the current time, and
enqueues a normal pipeline task for each match. Dependency-free: a tiny cron
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
    """Test whether a single cron field ``spec`` matches an integer ``value``.

    Supports the standard cron sub-syntaxes for one field: ``*`` / ``?`` (any),
    ``*/n`` (every n within the field's full range), ``a-b`` (inclusive range),
    ``a-b/n`` or ``*/n`` (stepped range), comma-separated lists of any of those,
    and a plain integer. ``lo``/``hi`` bound the field (e.g. 0..59 for minutes)
    and are used as the implicit start/end when the base is ``*`` or empty.

    Args:
        spec: The raw cron field text (e.g. ``"*/15"``, ``"1,3,5"``, ``"9-17"``).
        value: The current value of the corresponding datetime component.
        lo: Lowest legal value for this field (used to expand ``*``).
        hi: Highest legal value for this field (used to expand ``*``).

    Returns:
        True if any comma-separated part of ``spec`` matches ``value``.

    Notes:
        The step check ``(value - start) % step == 0`` is anchored at ``start``,
        matching standard cron semantics (e.g. ``5-20/5`` fires at 5, 10, 15, 20).
    """
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
    """True when ``dt`` (minute resolution) satisfies a 5-field cron expression.

    Args:
        expr: A standard 5-field cron string ``"minute hour dom month dow"``.
            A ``None``/empty/malformed expression (not exactly 5 fields) never
            matches and returns False rather than raising.
        dt: The datetime to test; only minute-and-coarser components are used.

    Returns:
        True only if every one of the 5 fields matches the corresponding
        component of ``dt``.

    Notes:
        Day-of-week handling bridges two conventions: cron treats both 0 and 7
        as Sunday, while Python's ``weekday()`` is Mon=0..Sun=6. We convert to
        ``py_dow`` (Sun=0..Sat=6) and additionally test the Sunday-as-7 form so
        expressions written either way match.
    """
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
    """Load the agents eligible to be fired by the scheduler.

    Reads the full agent registry on every call (so newly added/edited agents
    are picked up without a restart) and keeps only those that are active and
    carry a non-empty ``schedule_cron`` expression.

    Returns:
        A list of matching ``AgentRecord`` objects (possibly empty).

    Side effects:
        Calls ``load_all_agents()``, which performs registry I/O each tick.
    """
    return [r for r in load_all_agents() if r.active and r.schedule_cron]


def due_agents(now: datetime) -> list[AgentRecord]:
    """Return the scheduled agents whose cron expression fires at ``now``.

    Args:
        now: The reference time (minute resolution) to evaluate against.

    Returns:
        The subset of ``scheduled_agents()`` whose cron matches ``now``.
    """
    return [r for r in scheduled_agents() if cron_matches(r.schedule_cron or "", now)]


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
    per minute. Aligns the first tick to the next minute boundary.

    Args:
        enqueue_fn: Injected coroutine that turns a due ``AgentRecord`` into a
            pipeline task. Injected (rather than imported) to avoid an import
            cycle with the API layer.
        stop_event: Set by the FastAPI shutdown hook to break the loop. Also
            used as the sleep primitive so shutdown is immediate (no waiting out
            a full tick).
        tick_seconds: Polling interval; defaults to 60 to match the cron
            minute resolution.
        now_fn: Clock source, overridable in tests to drive deterministic time.

    Returns:
        None. Runs until ``stop_event`` is set.

    Notes:
        ``last_fired_minute`` maps agent id -> the ``YYYY-mm-ddTHH:MM`` key it
        last fired in, giving idempotency: even if the loop ticks several times
        within the same minute, each agent enqueues at most once per minute.
        Exceptions from a single ``enqueue_fn`` call are swallowed so one bad
        agent cannot kill the whole scheduler.
    """
    # agent id -> minute key of its last fire; enforces once-per-minute firing.
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
        # Sleep until the next tick OR until stop is requested, whichever first:
        # waiting on stop_event makes shutdown instant; the TimeoutError is the
        # normal "tick elapsed, loop again" path and is intentionally ignored.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass


def scheduled_task_request(record: AgentRecord) -> str:
    """The entry-point prompt a scheduled agent runs. Uses the agent's own
    description/goal so the run is meaningful without an external trigger.

    Args:
        record: The scheduled agent being fired.

    Returns:
        A prompt string prefixed with a ``[Scheduled run ...]`` marker so
        downstream logs/traces can distinguish cron-triggered runs.

    Notes:
        Body falls back ``description -> goal -> name`` so there is always some
        meaningful instruction text even for sparsely configured agents.
    """
    body = record.description or record.goal or record.name
    return f"[Scheduled run of agent '{record.name}'] {body}".strip()
