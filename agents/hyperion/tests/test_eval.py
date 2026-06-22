"""Prover eval/observability for the trimmed DAG."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperion.eval import lean_prove_benchmark
from hyperion.eval import thesis_curve
from hyperion.eval.demo import SAMPLE_PROBLEMS, run_problem
from hyperion.eval.trace import collect_trace, format_trace


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_collect_trace_infers_subgoals_from_blackboard_keys():
    bb = {
        "skeleton_ok": True,
        "candidate_a:h1": {"origin": "retrieve", "path": "A", "lean_type": "P", "proof_term": "pa"},
        "verified_a:h1": {"origin": "retrieve", "path": "A"},
        "discharged:h1": {"origin": "retrieve", "path": "A", "proof_term": "pa", "lean_type": "P"},
    }
    trace = collect_trace(request="prove P", blackboard=bb, plan=None,
                          result_lean="theorem t : P := pa", status="done")
    h1 = trace["subgoals"]["h1"]
    assert trace["status"] == "done"
    assert h1["candidate_a"]["proof_term"] == "pa"
    assert h1["discharged"]["path"] == "A"
    assert h1["candidates_a"] == []


def test_format_trace_renders_trimmed_stage_labels():
    bb = {
        "candidate_a:h1": {"origin": "retrieve", "path": "A", "lean_type": "P", "proof_term": "pa"},
        "verified_a:h1": {"path": "A"},
        "verify_decision:h1": {"a_attempts": 1, "repair_iters": 0},
        "escalated:h1": False,
        "discharged:h1": {"origin": "retrieve", "path": "A", "proof_term": "pa"},
    }
    text = format_trace(collect_trace(request="prove P", blackboard=bb, plan=None,
                                      result_lean="theorem t : P := pa", status="done"))
    for label in ("retrieve", "synthesize", "verify", "definition synth",
                  "prove through", "concept bank", "discharged",
                  "result.lean", "sub-goal h1"):
        assert label in text
    assert "compare" not in text
    assert "abstract" not in text
    assert "birth ablation" not in text


_OUTCOMES = [
    {"winner_path": "A", "reuse_depth": 1},
    {"winner_path": "B", "reuse_depth": 0},
    {"winner_path": "B", "reuse_depth": 0},
    {"winner_path": None, "reuse_depth": 0},
]


def test_aggregate_counts_and_rates():
    agg = thesis_curve.aggregate(_OUTCOMES)
    assert agg["n_subgoals"] == 4
    assert agg["solved"] == 3
    assert agg["solved_rate"] == pytest.approx(0.75)
    assert agg["path_a_wins"] == 1
    assert agg["path_b_wins"] == 2
    assert agg["path_a_win_rate"] == pytest.approx(1 / 3)
    assert agg["mean_reuse_depth"] == pytest.approx(1.0)
    assert agg["max_reuse_depth"] == 1
    assert agg["depth_histogram"] == {1: 1}


def test_aggregate_concepts_counts_banked_concepts():
    concepts = [{"concept_id": "c1", "bank_id": "pt1"}, {"concept_id": "c2"}]
    agg = thesis_curve.aggregate_concepts(concepts)
    assert agg["n_concepts"] == 2
    assert agg["banked_concepts"] == 1
    assert agg["banked_concept_rate"] == pytest.approx(0.5)


def test_running_curve_only_advances_on_solved():
    assert thesis_curve.running_curve(_OUTCOMES) == [
        pytest.approx(1.0), pytest.approx(0.5), pytest.approx(1 / 3)
    ]


def test_paired_row_marks_definition_escalation_rescue():
    row = lean_prove_benchmark._paired_row(
        {"id": "hard"},
        {"status": "failed", "path_c_wins": 0},
        {"status": "done", "path_c_wins": 1},
        eval_mode="dev",
        lean_profile="core",
    )
    assert row["case_id"] == "hard"
    assert row["rescued_by_escalation"] is True


def test_benchmark_body_prefers_formal_statement():
    body = lean_prove_benchmark._task_body(
        {"prompt": "prove theorem foo : True", "formal_statement": "theorem foo : True := by sorry"},
        eval_mode="dev",
        order_seed=None,
        prover_definition_escalation=True,
    )
    assert body["task"] == "theorem foo : True := by sorry"


def test_hard_smoke_fixture_schema():
    path = Path(__file__).parents[1] / "evals" / "lean_prove_splits" / "hard_smoke.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert len(rows) >= 3
    for row in rows:
        assert row["id"].startswith("hard_")
        assert row["split"] == "hard_smoke"
        assert row["workflow"] == "lean-prove"
        assert row["lean_profile"] == "core"
        assert row["expected"] == "escalation_on_only"
        assert row["source"] == "curated-hard-smoke"
        assert row["formal_statement"].startswith("theorem hard_")
        assert row["formal_statement"].endswith("by sorry")
        assert "definition-escalation" in row["tags"]
        assert "sorry" in row["prompt"].lower()


@pytest.mark.anyio
async def test_demo_conjunction_runs_and_traces_both_paths(tmp_path):
    problem = next(p for p in SAMPLE_PROBLEMS if p.id == "demo_conjunction")
    result, trace = await run_problem(problem, tmp_path)

    assert result["status"] == "done"
    assert trace["result_lean"] is not None and "sorry" not in trace["result_lean"]
    assert trace["subgoals"]["h1"]["discharged"]["path"] == "A"
    assert trace["subgoals"]["h2"]["discharged"]["path"] == "B"


@pytest.mark.anyio
async def test_demo_repair_loop_fires(tmp_path):
    problem = next(p for p in SAMPLE_PROBLEMS if p.id == "demo_repair")
    result, trace = await run_problem(problem, tmp_path)

    assert result["status"] == "done"
    assert trace["subgoals"]["h1"]["verify_decision"]["repair_iters"] >= 1
    assert trace["subgoals"]["h1"]["discharged"]["origin"] == "repair"


@pytest.mark.anyio
async def test_thesis_summary_over_demo_runs(tmp_path):
    outcomes = []
    for problem in SAMPLE_PROBLEMS:
        _result, trace = await run_problem(problem, tmp_path)
        outcomes += [sg["discharged"] for sg in trace["subgoals"].values() if sg.get("discharged")]

    agg = thesis_curve.aggregate(outcomes)
    assert agg["n_subgoals"] == 3
    assert agg["solved"] == 3
    summary = thesis_curve.format_summary(outcomes, [{"concept_id": "c1", "bank_id": "pt1"}])
    assert "running A win-rate" in summary
    assert "concepts" in summary
