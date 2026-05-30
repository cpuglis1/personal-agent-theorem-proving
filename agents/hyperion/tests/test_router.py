"""Routing engine + plan-contract tests (PLAN_UNIFIED.md Phase 2)."""

from __future__ import annotations

import pytest

from hyperion.agents.registry import AgentRecord, Trigger
from hyperion.crews.router import RoutingError, route_work


def _work(id_, trigger, active=True, order=1):
    return AgentRecord(
        id=id_, name=id_, stage="work", role=id_, goal="g", backstory="b",
        active=active, order=order, trigger=trigger,
    )


def test_keyword_trigger_fires_only_on_match():
    dev = _work("developer", Trigger(type="keyword", keywords=["code"]))
    # request mentions code -> selected
    r = route_work([dev], "please write some code for me")
    assert [a.id for a in r.selected] == ["developer"]
    # request without the keyword -> skipped with reason
    r2 = route_work([dev], "research the market")
    assert r2.selected == []
    assert any(s["id"] == "developer" and "keyword" in s["reason"] for s in r2.skipped)


def test_task_type_trigger():
    dev = _work("developer", Trigger(type="task_type", task_types=["code"]))
    assert route_work([dev], "x", task_type="code").selected[0].id == "developer"
    assert route_work([dev], "x", task_type="research").selected == []


def test_upstream_runs_after_dep():
    base = _work("researcher", Trigger(type="always"), order=1)
    dependent = _work("writer", Trigger(type="upstream", upstream=["researcher"]), order=2)
    r = route_work([dependent, base], "anything")
    ids = [a.id for a in r.selected]
    assert ids == ["researcher", "writer"]  # dep first despite declaration order
    assert r.dag["writer"] == ["researcher"]


def test_upstream_not_selected_is_skipped():
    # upstream dep is inactive -> dependent cannot fire
    base = _work("researcher", Trigger(type="always"), active=False)
    dependent = _work("writer", Trigger(type="upstream", upstream=["researcher"]))
    r = route_work([base, dependent], "x")
    assert r.selected == []
    assert any(s["id"] == "writer" for s in r.skipped)


def test_cycle_rejected():
    a = _work("a", Trigger(type="upstream", upstream=["b"]))
    b = _work("b", Trigger(type="upstream", upstream=["a"]))
    # Neither fires (upstream never satisfied), so no cycle is reached via triggers.
    # Force selection by making both 'always' but keep the cyclic upstream edges.
    a2 = _work("a", Trigger(type="always", upstream=["b"]))
    b2 = _work("b", Trigger(type="always", upstream=["a"]))
    with pytest.raises(RoutingError):
        route_work([a2, b2], "x")


def test_inactive_skipped_with_reason():
    on = _work("researcher", Trigger(type="always"))
    off = _work("developer", Trigger(type="always"), active=False)
    r = route_work([on, off], "x")
    assert [a.id for a in r.selected] == ["researcher"]
    assert any(s["id"] == "developer" and s["reason"] == "inactive" for s in r.skipped)


def test_parse_plan_graceful_defaults(tmp_path):
    from hyperion.config import settings
    from unittest.mock import patch
    from hyperion.crews.plan_contract import parse_plan

    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "t1").mkdir()
        (tmp_path / "t1" / "plan.md").write_text(
            "---\ntask_type: code\nkeywords: [python, docker]\nneeds_review: true\n---\n\n# Plan\n"
        )
        fm = parse_plan("t1")
        assert fm.task_type == "code"
        assert fm.keywords == ["python", "docker"]
        assert fm.needs_review is True
        # missing plan -> defaults
        assert parse_plan("nope").task_type == "mixed"
