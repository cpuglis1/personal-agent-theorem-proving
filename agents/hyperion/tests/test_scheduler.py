"""Scheduler cron-matcher + due-agent tests (PLAN_UNIFIED.md Phase 8)."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from hyperion import scheduler


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_every_5_minutes_matches_on_the_fives():
    expr = "*/5 * * * *"
    assert scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 0))
    assert scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 5))
    assert scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 55))
    assert not scheduler.cron_matches(expr, datetime(2026, 5, 29, 10, 3))


def test_exact_and_range_and_list():
    assert scheduler.cron_matches("30 9 * * *", datetime(2026, 5, 29, 9, 30))
    assert not scheduler.cron_matches("30 9 * * *", datetime(2026, 5, 29, 10, 30))
    assert scheduler.cron_matches("0 9-17 * * *", datetime(2026, 5, 29, 13, 0))
    assert not scheduler.cron_matches("0 9-17 * * *", datetime(2026, 5, 29, 18, 0))
    assert scheduler.cron_matches("0 0 * * 1,3,5", datetime(2026, 5, 29, 0, 0))  # Fri


def test_malformed_expr_never_matches():
    assert not scheduler.cron_matches("not a cron", datetime(2026, 5, 29, 0, 0))
    assert not scheduler.cron_matches("", datetime(2026, 5, 29, 0, 0))


@pytest.mark.anyio
async def test_loop_enqueues_due_agent_once_per_minute(monkeypatch):
    fired: list[str] = []

    class FakeRec:
        id = "newsbot"

    monkeypatch.setattr(scheduler, "due_agents", lambda now: [FakeRec()])

    async def enqueue(record):
        fired.append(record.id)

    stop = asyncio.Event()
    fixed = datetime(2026, 5, 29, 10, 5)
    task = asyncio.create_task(
        scheduler.run_scheduler(
            enqueue, stop_event=stop, tick_seconds=0.05, now_fn=lambda: fixed
        )
    )
    await asyncio.sleep(0.25)
    stop.set()
    await task
    # Same minute across many ticks → enqueued exactly once.
    assert fired == ["newsbot"]
