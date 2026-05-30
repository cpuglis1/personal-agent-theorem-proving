"""Context-layer tests (PLAN_UNIFIED.md Phase 4): blackboard + prioritize budget."""

from __future__ import annotations

from unittest.mock import patch

from hyperion.memory import context_store
from hyperion.tools import reranker


def test_context_put_get_roundtrip(tmp_path):
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        context_store.context_put("t1", "headline", "42%")
        context_store.context_put("t1", "source", "https://example.com")
        assert context_store.context_get("t1", "headline") == "42%"
        # whole-blackboard read
        assert context_store.context_get("t1") == {
            "headline": "42%",
            "source": "https://example.com",
        }
        # missing key → None
        assert context_store.context_get("t1", "nope") is None


def test_context_get_missing_task_returns_empty(tmp_path):
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        assert context_store.context_get("ghost") == {}
        assert context_store.context_get("ghost", "k") is None


def test_synthesizer_reads_fact_it_did_not_produce(tmp_path):
    """Acceptance: a downstream stage reads a context fact written upstream."""
    from hyperion.config import settings

    with patch.object(settings, "tasks_dir", tmp_path):
        # researcher writes
        context_store.ContextPutTool(task_id="t2")._run("finding", "GDP grew 3%")
        # synthesizer reads
        out = context_store.ContextGetTool(task_id="t2")._run("finding")
        assert "GDP grew 3%" in out


def test_prioritize_trims_to_token_budget():
    # Reranker is offline in tests → falls back to original order with score 0.0,
    # so prioritize keeps items in order until the budget is hit.
    candidates = ["x" * 400, "y" * 400, "z" * 400]  # ≈100 tokens each
    with patch.object(reranker, "rerank", return_value=[(0, 0.0), (1, 0.0), (2, 0.0)]):
        kept = reranker.prioritize("q", candidates, token_budget=150)
    # first (100) fits; second would push to 200 > 150 → dropped; always keep ≥1
    assert kept == [candidates[0]]


def test_prioritize_keeps_at_least_one_over_budget():
    big = ["a" * 10_000]  # far over budget
    with patch.object(reranker, "rerank", return_value=[(0, 0.0)]):
        kept = reranker.prioritize("q", big, token_budget=10)
    assert kept == big  # never drop the single best candidate


def test_prioritize_empty():
    assert reranker.prioritize("q", []) == []
