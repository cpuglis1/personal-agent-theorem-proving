"""Unit tests for Hyperion's agent scheduler (PLAN_UNIFIED.md Phase 8).

The scheduler (``hyperion.scheduler``) is responsible for triggering agent runs
on a recurring basis using standard 5-field cron expressions. This suite covers
the two pieces that make that work:

1. ``scheduler.cron_matches(expr, dt)`` — a pure, dependency-free cron-expression
   matcher. These tests pin down its handling of the ``*/N`` step syntax, exact
   values, ranges (``a-b``), comma-separated lists (``a,b,c``), and malformed /
   empty expressions (which must fail closed and never match).

2. ``scheduler.run_scheduler(...)`` — the async polling loop that ticks on a
   short interval, asks ``scheduler.due_agents(now)`` which agents are due, and
   enqueues each one. The key behavior verified here is de-duplication within a
   single clock minute: even though the loop ticks many times per minute, a due
   agent must be enqueued exactly once per minute (not once per tick).

Notes:
- All dates use 2026-05-29, a Friday, which matters for the day-of-week
  assertions (cron weekday field ``1,3,5`` = Mon/Wed/Fri).
- The async test runs against the asyncio backend (see the ``anyio_backend``
  fixture) and uses ``monkeypatch`` plus an injected ``now_fn`` to make the loop
  fully deterministic without touching the real system clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from hyperion import scheduler


@pytest.fixture
def anyio_backend():
    """Force anyio-powered async tests to run on the asyncio backend.

    Returns:
        str: The backend name ("asyncio") that ``pytest.mark.anyio`` will use to
        drive coroutine-based tests, ensuring they run under asyncio rather than
        anyio's other supported backends (e.g. trio).
    """
    return "asyncio"


def test_every_5_minutes_matches_on_the_fives():
    """The ``*/5`` minute step matches only minutes divisible by 5.

    Verifies that "*/5 * * * *" matches at minutes 0, 5, and 55 (all multiples
    of 5) and does not match at minute 3, confirming correct interpretation of
    the cron step syntax in the minute field.
    """
    expr = "*/5 * * * *"
    assert scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 0))
    assert scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 5))
    assert scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 55))
    assert not scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 3))


def test_exact_and_range_and_list():
    """Exact values, hour ranges, and weekday lists are all matched correctly.

    Covers three cron field forms in one test: an exact minute+hour ("30 9"
    matches 09:30 but not 10:30), an hour range ("9-17" matches 13:00 but not
    18:00 since the range is inclusive-exclusive of the bounds tested), and a
    day-of-week list ("1,3,5" matches Friday 2026-05-29).
    """
    assert scheduler.cron_matches("30 9 * * *", datetime(2026, 5, 29, 9, 30))
    assert not scheduler.cron_matches("30 9 * * *", datetime(2026, 5, 29, 10, 30))
    assert scheduler.cron_matches("0 9-17 * * *", datetime(2026, 5, 29, 13, 0))
    assert not scheduler.cron_matches("0 9-17 * * *", datetime(2026, 5, 29, 18, 0))
    assert scheduler.cron_matches("0 0 * * 1,3,5", datetime(2026, 5, 29, 0, 0))  # Fri


def test_malformed_expr_never_matches():
    """Invalid or empty cron expressions fail closed and never match.

    Ensures the matcher treats garbage input ("not a cron") and the empty string
    as non-matching rather than raising or accidentally matching, so a bad
    schedule config can never silently trigger runs at every tick.
    """
    assert not scheduler.cron_matches("not a cron", datetime(2026, 5, 29, 0, 0))
    assert not scheduler.cron_matches("", datetime(2026, 5, 29, 0, 0))


@pytest.mark.anyio
async def test_loop_enqueues_due_agent_once_per_minute(monkeypatch):
    """The scheduler loop enqueues each due agent only once per clock minute.

    With ``now`` frozen to a single minute and ``due_agents`` always returning
    the same agent, the loop ticks many times (every 0.05s for ~0.25s) but must
    de-duplicate within the minute so the agent is enqueued exactly once. Guards
    against the loop re-firing a schedule on every tick.

    Args:
        monkeypatch: pytest fixture used to replace ``scheduler.due_agents`` with
            a stub that always reports the fake agent as due.

    Side effects:
        Spawns and then cleanly stops a background asyncio task running the
        scheduler loop.
    """
    # Records the ids of every agent the loop enqueues, so we can assert on count.
    fired: list[str] = []

    class FakeRec:
        """Minimal stand-in for a scheduled-agent record exposing only ``id``."""

        id = "newsbot"

    # Pretend exactly one agent is always due, regardless of the passed-in time.
    monkeypatch.setattr(scheduler, "due_agents", lambda now: [FakeRec()])

    async def enqueue(record):
        """Test double for the loop's enqueue callback; logs the agent id.

        Args:
            record: The due-agent record the scheduler is enqueuing.
        """
        fired.append(record.id)

    stop = asyncio.Event()
    # Freeze "now" to a single minute so de-duplication-per-minute is testable;
    # every tick sees the same minute and must not re-enqueue.
    fixed = datetime(2026, 5, 29, 10, 5)
    task = asyncio.create_task(
        scheduler.run_scheduler(
            enqueue, stop_event=stop, tick_seconds=0.05, now_fn=lambda: fixed
        )
    )
    # Let the loop run for several ticks (0.25s / 0.05s ≈ 5 ticks) before stopping.
    await asyncio.sleep(0.25)
    stop.set()
    await task
    # Same minute across many ticks → enqueued exactly once.
    assert fired == ["newsbot"]
