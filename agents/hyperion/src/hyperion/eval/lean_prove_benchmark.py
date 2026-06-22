"""Lean-prove benchmark runner.

This module submits JSONL benchmark cases to the Hyperion API and records terminal task
summaries. It is intentionally inert unless invoked by an operator. Use it only after the
desired split/mode is approved.

Example:

    python -m hyperion.eval.lean_prove_benchmark \
      --cases agents/hyperion/evals/lean_prove_splits/dev.jsonl \
      --eval-mode dev \
      --out agents/hyperion/tasks/dev-results.jsonl
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
            body = {
                "task": case["prompt"],
                "workflow": case.get("workflow") or "lean-prove",
                "hitl": "off",
                "eval_mode": eval_mode,
                "lean_profile": case.get("lean_profile") or "core",
                "problem_id": case.get("id"),
                "split": case.get("split"),
                "order_seed": order_seed,
                "cap_wall_seconds": case.get("cap_wall_seconds", 600),
            }
            created = _json_request("POST", "/tasks", body)
            task_id = created["task_id"]
            while True:
                task = _json_request("GET", f"/tasks/{task_id}")
                if task.get("status") in {"done", "failed"}:
                    break
                time.sleep(poll_seconds)
            trace = _json_request("GET", f"/tasks/{task_id}/trace")
            prover = trace.get("prover") or {}
            row = {
                "case_id": case.get("id"),
                "task_id": task_id,
                "eval_mode": eval_mode,
                "lean_profile": body["lean_profile"],
                "status": task.get("status"),
                "error": task.get("error"),
                "final_verify": prover.get("final_verify"),
                "formal_statement_ingestion": prover.get("formal_statement_ingestion"),
                "subgoal_unbound_context": prover.get("subgoal_unbound_context"),
                "n_subgoals": len(prover.get("subgoals") or {}),
                "trace_events": len(trace.get("events") or []),
            }
            out.write(json.dumps(row, sort_keys=True) + "\n")
            out.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--eval-mode", choices=["train", "dev", "test"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--order-seed", type=int)
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()

    if args.eval_mode == "test":
        raise SystemExit("Refusing to run final test from this helper without explicit code change.")
    run_cases(
        cases_path=args.cases,
        eval_mode=args.eval_mode,
        out_path=args.out,
        order_seed=args.order_seed,
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    main()
