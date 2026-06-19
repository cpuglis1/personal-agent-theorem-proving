"""Native-stage trace rows — the deterministic prover nodes (skeleton_check/retrieve/
verify/compare/abstract/bank) make no LLM call, so without an explicit trace row they
render as dimmed/empty nodes in the Trace Flow UI. ``record_native_stage`` writes one
``trace_events`` row per native node so each stage shows as FIRED with a readable output.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from hyperion import usage


def test_summary_phrasings_are_human_readable():
    s = usage._native_stage_summary
    assert "type-checks" in s("skeleton_check", {"ok": True})
    assert "FAILED" in s("skeleton_check", {"ok": False, "errors": ["e"]})
    assert "2 applicable" in s("retrieve", {"n_candidates": 2, "has_candidate": True})
    assert "winner=Path B · A-attempts=0 · repair-iters=1" in s(
        "verify", {"winner_path": "B",
                   "decision": {"a_attempts": 0, "repair_iters": 1, "mode": "deploy"}})
    assert "compared=True" in s("compare", {"winner_path": "A", "compared": True})
    assert "abstracted=True" in s("abstract", {"abstracted": True, "n_rejected": 1})
    assert "banked 1/1" in s("bank", {"n_banked": 1, "n_discharged": 1})
    # Unknown handler falls back to compact JSON, never raises.
    assert "foo" in s("mystery", {"foo": 1})


def test_record_native_stage_writes_a_trace_row(tmp_path):
    """A native stage is persisted as a trace_events row keyed by node_id (UI groups on it)."""
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as c:
        c.execute(
            """CREATE TABLE trace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                agent_role TEXT NOT NULL, node_id TEXT, prompt_type TEXT,
                model TEXT, input_tokens INT, output_tokens INT, cost_usd REAL,
                prompt_preview TEXT, response_preview TEXT, tools_used TEXT,
                started_at TEXT, duration_ms INT)"""
        )

    class _S:
        tasks_dir = tmp_path

    with patch.object(usage, "settings", _S, create=True), \
         patch("hyperion.config.settings", _S, create=True):
        usage.record_native_stage("t1", "bank", "bank",
                                   {"n_banked": 1, "n_discharged": 1}, duration_ms=12)

    with sqlite3.connect(db) as c:
        rows = c.execute(
            "SELECT task_id, agent_role, node_id, prompt_type, model, response_preview, "
            "duration_ms FROM trace_events"
        ).fetchall()
    assert len(rows) == 1
    task_id, role, node_id, ptype, model, preview, dur = rows[0]
    assert (task_id, role, node_id, ptype, model, dur) == (
        "t1", "native/bank", "bank", "native-stage", "native", 12)
    assert "banked 1/1" in preview


def test_record_native_stage_never_raises():
    """A tracing failure (e.g. no DB) must never propagate into the run."""
    usage.record_native_stage("t", "n", "bank", {}, duration_ms=None)  # no patch → swallowed
