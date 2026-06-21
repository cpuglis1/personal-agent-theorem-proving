"""Prover eval/observability — the stage tracer, the thesis-curve aggregator, the demo.

Build-plan Post-work: the triple log is the dataset and this is its read-out; the tracer is
the "see how each stage performs/outputs" instrument. All offline (the demo runner mocks LLM
+ Lean), mirroring the rest of the suite.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hyperion.config import settings
from hyperion.eval import thesis_curve
from hyperion.eval.demo import SAMPLE_PROBLEMS, Problem, run_problem
from hyperion.eval.trace import collect_trace, format_trace


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# trace.collect_trace / format_trace (pure)
# ---------------------------------------------------------------------------


def test_collect_trace_infers_subgoals_from_blackboard_keys():
    bb = {
        "skeleton_ok": True,
        "candidate_a:h1": {"origin": "retrieve", "path": "A", "lean_type": "P", "proof_term": "pa"},
        "verified_a:h1": {"origin": "retrieve", "path": "A"},
        "triple_log:h1": {"winner_path": "A", "compared": False, "scores": {"a": 0.0}},
        "discharged:h1": {"origin": "retrieve", "path": "A", "proof_term": "pa", "lean_type": "P"},
        "abstracted:h1": None,
    }
    trace = collect_trace(request="prove P", blackboard=bb, plan=None,
                          result_lean="theorem t : P := pa", status="done")
    assert trace["status"] == "done"
    assert trace["skeleton_ok"] is True
    assert set(trace["subgoals"]) == {"h1"}
    h1 = trace["subgoals"]["h1"]
    assert h1["candidate_a"]["proof_term"] == "pa"
    assert h1["triple_log"]["winner_path"] == "A"
    assert h1["candidates_a"] == []  # missing key defaults to []


def test_format_trace_renders_each_stage_label():
    bb = {
        "candidate_a:h1": {"origin": "retrieve", "path": "A", "lean_type": "P", "proof_term": "pa"},
        "verified_a:h1": {"path": "A"},
        "verify_decision:h1": {"mode": "deploy", "a_attempts": 1, "repair_iters": 0},
        "escalated:h1": False,
        "triple_log:h1": {"winner_path": "A", "compared": False, "scores": {"a": 0.0}},
        "discharged:h1": {"origin": "retrieve", "path": "A", "proof_term": "pa"},
    }
    text = format_trace(collect_trace(request="prove P", blackboard=bb, plan=None,
                                      result_lean="theorem t : P := pa", status="done"))
    for label in ("retrieve", "synthesize", "verify", "compare", "abstract", "discharged",
                  "definition synth", "birth ablation", "concept bank",
                  "result.lean", "sub-goal h1"):
        assert label in text


# ---------------------------------------------------------------------------
# thesis_curve aggregation (pure math)
# ---------------------------------------------------------------------------

_TRIPLES = [
    {"winner_path": "A", "compared": True, "reuse_depth": 1},   # contest, retrieval won (breadth)
    {"winner_path": "B", "compared": True, "reuse_depth": 0},   # contest, synthesis won
    {"winner_path": "B", "compared": False, "reuse_depth": 0},  # only B verified
    {"winner_path": None, "compared": False, "reuse_depth": 0}, # unsolved
]


def test_aggregate_counts_and_rates():
    agg = thesis_curve.aggregate(_TRIPLES)
    assert agg["n_subgoals"] == 4
    assert agg["solved"] == 3
    assert agg["solved_rate"] == pytest.approx(0.75)
    assert agg["path_a_wins"] == 1
    assert agg["path_b_wins"] == 2
    assert agg["path_a_win_rate"] == pytest.approx(1 / 3)
    assert agg["n_contests"] == 2
    assert agg["retrieval_beats_synthesis_in_contest"] == pytest.approx(0.5)
    assert agg["mean_reuse_depth"] == pytest.approx(1.0)   # the one A-win had depth 1
    assert agg["max_reuse_depth"] == 1
    assert agg["depth_histogram"] == {1: 1}


def test_aggregate_depth_separates_breadth_from_depth():
    # Two A-wins: one breadth (depth 1), one depth (depth 3) ⇒ mean 2, max 3, histogram split.
    triples = [
        {"winner_path": "A", "compared": False, "reuse_depth": 1},
        {"winner_path": "A", "compared": False, "reuse_depth": 3},
        {"winner_path": "B", "compared": False, "reuse_depth": 0},  # ignored (not Path A)
    ]
    agg = thesis_curve.aggregate(triples)
    assert agg["mean_reuse_depth"] == pytest.approx(2.0)
    assert agg["max_reuse_depth"] == 3
    assert agg["depth_histogram"] == {1: 1, 3: 1}
    # The running depth curve trends up: 1.0 then mean(1,3)=2.0.
    assert thesis_curve.depth_curve(triples) == [pytest.approx(1.0), pytest.approx(2.0)]


def test_aggregate_weak_gate_necessity_and_counterfactual():
    # Two A-wins: one was necessary (no eligible weak B, but strong B existed ⇒ gated),
    # one was merely preferred (a weak B also verified). Plus one B win.
    triples = [
        {"winner_path": "A", "synthesized_verified": False, "path_b_gated": True},   # necessary
        {"winner_path": "A", "synthesized_verified": True, "path_b_gated": False},   # preferred
        {"winner_path": "B", "synthesized_verified": True, "path_b_gated": False},
    ]
    agg = thesis_curve.aggregate(triples)
    assert agg["path_a_necessary"] == 1
    assert agg["path_a_necessary_rate"] == pytest.approx(0.5)   # 1 of 2 A-wins
    assert agg["n_path_b_gated"] == 1


def test_aggregate_empty_is_all_zero():
    agg = thesis_curve.aggregate([])
    assert agg["solved_rate"] == 0.0 and agg["path_a_win_rate"] == 0.0
    assert agg["mean_reuse_depth"] == 0.0 and agg["max_reuse_depth"] == 0
    assert agg["depth_histogram"] == {}
    assert agg["path_a_necessary_rate"] == 0.0 and agg["n_path_b_gated"] == 0


def test_aggregate_concepts_counts_certified_reuse(monkeypatch):
    monkeypatch.setattr(settings, "concept_promote_k", 2)
    concepts = [
        {"concept_id": "c1", "necessity_hits": 2, "provisional": False, "bank_id": "pt1"},
        {"concept_id": "c2", "necessity_hits": 1, "provisional": True},
    ]
    agg = thesis_curve.aggregate_concepts(concepts)
    assert agg["n_concepts"] == 2
    assert agg["banked_concepts"] == 1
    assert agg["certified_reusable_concepts"] == 1
    assert agg["certified_reusable_rate"] == pytest.approx(0.5)
    assert agg["total_necessity_hits"] == 3


def test_running_curve_only_advances_on_solved():
    # A, B, B, (unsolved skipped) → cumulative A-rate: 1/1, 1/2, 1/3.
    curve = thesis_curve.running_curve(_TRIPLES)
    assert curve == [pytest.approx(1.0), pytest.approx(0.5), pytest.approx(1 / 3)]


# ---------------------------------------------------------------------------
# demo.run_problem — the real runner, mocked LLM/Lean, traced
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_demo_conjunction_runs_and_traces_both_paths(tmp_path):
    """The conjunction sample: h1 closes via Path A (retrieval), h2 via Path B (synthesis);
    the assembled result.lean is sorry-free and the trace records both paths."""
    problem = next(p for p in SAMPLE_PROBLEMS if p.id == "demo_conjunction")
    result, trace = await run_problem(problem, tmp_path)

    assert result["status"] == "done"
    assert trace["result_lean"] is not None and "sorry" not in trace["result_lean"]
    assert trace["subgoals"]["h1"]["discharged"]["path"] == "A"
    assert trace["subgoals"]["h2"]["discharged"]["path"] == "B"


@pytest.mark.anyio
async def test_demo_repair_loop_fires(tmp_path):
    """The repair sample: a `sorry`-bearing synthesized candidate fails `full`, the repair
    loop proposes a sorry-free fix, and the kernel then accepts it (repair_iters >= 1)."""
    problem = next(p for p in SAMPLE_PROBLEMS if p.id == "demo_repair")
    result, trace = await run_problem(problem, tmp_path)

    assert result["status"] == "done"
    assert trace["subgoals"]["h1"]["verify_decision"]["repair_iters"] >= 1
    assert trace["subgoals"]["h1"]["discharged"]["origin"] == "repair"


@pytest.mark.anyio
async def test_demo_research_mode_is_a_contest_and_abstracts(tmp_path):
    """The research sample: both paths verify (`compared` True) and the abstractor fires on
    the fresh Path-B lemma, producing a generalized (binder-bearing) form."""
    problem = next(p for p in SAMPLE_PROBLEMS if p.id == "demo_research_abstract")
    result, trace = await run_problem(problem, tmp_path)

    assert result["status"] == "done"
    h1 = trace["subgoals"]["h1"]
    assert h1["triple_log"]["compared"] is True
    assert h1["abstracted"]["origin"] == "abstract"        # generalized, not fallback
    assert "∀" in h1["abstracted"]["lean_type"]


@pytest.mark.anyio
async def test_thesis_summary_over_demo_runs(tmp_path):
    """End-to-end: run all sample problems, collect their triple logs, and confirm the
    thesis read-out aggregates them (all sub-goals solved)."""
    triples = []
    for problem in SAMPLE_PROBLEMS:
        _result, trace = await run_problem(problem, tmp_path)
        triples += [sg["triple_log"] for sg in trace["subgoals"].values() if sg.get("triple_log")]

    agg = thesis_curve.aggregate(triples)
    assert agg["n_subgoals"] == 4
    assert agg["solved"] == 4
    summary = thesis_curve.format_summary(triples, [{"concept_id": "c1", "necessity_hits": 2, "provisional": False}])
    assert "running A win-rate" in summary
    assert "concepts" in summary
