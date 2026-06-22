"""Lean-prove benchmark runner.

This module submits JSONL benchmark cases to the Hyperion API and records terminal task
summaries. It is intentionally inert unless invoked by an operator. Use it only after the
desired split/mode is approved.

Example:

    python -m hyperion.eval.lean_prove_benchmark \
      --cases agents/hyperion/evals/lean_prove_splits/dev.jsonl \
      --eval-mode dev \
      --out agents/hyperion/tasks/dev-results.jsonl \
      --paired-off-on
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from hyperion.config import settings


def _json_request(method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 90) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(
        settings.hyperion_api_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _iter_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _subgoal_values(prover: dict[str, Any]) -> list[dict[str, Any]]:
    subgoals = prover.get("subgoals") or {}
    return [sg for sg in subgoals.values() if isinstance(sg, dict)]


def _outcome_summary(*, task: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """Compact, stable row fragment for OFF/ON prover comparisons."""
    prover = trace.get("prover") or {}
    subgoals = _subgoal_values(prover)
    discharged = [sg.get("discharged") for sg in subgoals if isinstance(sg.get("discharged"), dict)]
    path_c = [d for d in discharged if d.get("path") == "C"]
    escalated = [sg for sg in subgoals if sg.get("escalated") is True]
    concepts_verified = [sg for sg in subgoals if isinstance(sg.get("verified_concept"), dict)]
    return {
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "error": task.get("error"),
        "final_verify": prover.get("final_verify"),
        "n_subgoals": len(subgoals),
        "n_discharged": len(discharged),
        "path_c_wins": len(path_c),
        "escalations": len(escalated),
        "concepts_verified": len(concepts_verified),
        "trace_events": len(trace.get("events") or []),
    }


def _paired_row(case: dict[str, Any], off: dict[str, Any], on: dict[str, Any], *, eval_mode: str,
                lean_profile: str) -> dict[str, Any]:
    return {
        "case_id": case.get("id"),
        "eval_mode": eval_mode,
        "lean_profile": lean_profile,
        "off": off,
        "on": on,
        "rescued_by_escalation": off.get("status") != "done"
        and on.get("status") == "done"
        and int(on.get("path_c_wins") or 0) > 0,
    }


def _task_body(case: dict[str, Any], *, eval_mode: str, order_seed: int | None,
               prover_definition_escalation: bool) -> dict[str, Any]:
    return {
        "task": case["prompt"],
        "workflow": case.get("workflow") or "lean-prove",
        "hitl": "off",
        "eval_mode": eval_mode,
        "lean_profile": case.get("lean_profile") or "core",
        "problem_id": case.get("id"),
        "split": case.get("split"),
        "order_seed": order_seed,
        "cap_wall_seconds": case.get("cap_wall_seconds", 600),
        "prover_definition_escalation": prover_definition_escalation,
    }


def _run_one(body: dict[str, Any], *, poll_seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
    created = _json_request("POST", "/tasks", body)
    task_id = created["task_id"]
    while True:
        task = _json_request("GET", f"/tasks/{task_id}")
        if task.get("status") in {"done", "failed"}:
            break
        time.sleep(poll_seconds)
    trace = _json_request("GET", f"/tasks/{task_id}/trace")
    return task, trace


def run_cases(
    *,
    cases_path: Path,
    eval_mode: str,
    out_path: Path,
    order_seed: int | None,
    poll_seconds: int = 5,
) -> None:
    """Submit cases and write one JSON result line per task."""
    cases = _iter_cases(cases_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as out:
        for case in cases:
            body = _task_body(
                case,
                eval_mode=eval_mode,
                order_seed=order_seed,
                prover_definition_escalation=case.get("prover_definition_escalation", True),
            )
            task, trace = _run_one(body, poll_seconds=poll_seconds)
            prover = trace.get("prover") or {}
            summary = _outcome_summary(task=task, trace=trace)
            row = {
                "case_id": case.get("id"),
                "eval_mode": eval_mode,
                "lean_profile": body["lean_profile"],
                "formal_statement_ingestion": prover.get("formal_statement_ingestion"),
                "subgoal_unbound_context": prover.get("subgoal_unbound_context"),
                **summary,
            }
            out.write(json.dumps(row, sort_keys=True) + "\n")
            out.flush()


def run_cases_paired_off_on(
    *,
    cases_path: Path,
    eval_mode: str,
    out_path: Path,
    order_seed: int | None,
    poll_seconds: int = 5,
) -> None:
    """Submit every case twice: definition escalation OFF, then ON."""
    cases = _iter_cases(cases_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as out:
        for case in cases:
            lean_profile = case.get("lean_profile") or "core"
            off_body = _task_body(
                case,
                eval_mode=eval_mode,
                order_seed=order_seed,
                prover_definition_escalation=False,
            )
            on_body = _task_body(
                case,
                eval_mode=eval_mode,
                order_seed=order_seed,
                prover_definition_escalation=True,
            )
            off_task, off_trace = _run_one(off_body, poll_seconds=poll_seconds)
            on_task, on_trace = _run_one(on_body, poll_seconds=poll_seconds)
            row = _paired_row(
                case,
                _outcome_summary(task=off_task, trace=off_trace),
                _outcome_summary(task=on_task, trace=on_trace),
                eval_mode=eval_mode,
                lean_profile=lean_profile,
            )
            out.write(json.dumps(row, sort_keys=True) + "\n")
            out.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--eval-mode", choices=["train", "dev", "test"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--order-seed", type=int)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--paired-off-on", action="store_true")
    args = parser.parse_args()

    if args.eval_mode == "test":
        raise SystemExit("Refusing to run final test from this helper without explicit code change.")
    kwargs = {
        "cases_path": args.cases,
        "eval_mode": args.eval_mode,
        "out_path": args.out,
        "order_seed": args.order_seed,
        "poll_seconds": args.poll_seconds,
    }
    if args.paired_off_on:
        run_cases_paired_off_on(**kwargs)
    else:
        run_cases(**kwargs)


if __name__ == "__main__":
    main()
