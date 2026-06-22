#!/usr/bin/env python3
"""Classify existing lean-prove run artifacts without launching new work.

The script is intentionally read-only over task artifacts. It reads task SQLite
rows, context.json, progress.log, plan.md, and benchmark result jsonl files, then
tags each terminal or partial run with a coarse failure/contamination class.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


STAGES = (
    "formal_ingest",
    "decompose",
    "skeleton_check",
    "retrieve",
    "synthesize",
    "verify",
    "compare",
    "escalation_gate",
    "abstract",
    "synthesize_definition",
    "verify_concept",
    "birth_ablation",
    "bank_concept",
    "bank",
)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_db_rows(root: Path) -> dict[str, dict[str, Any]]:
    db_path = root / "state.db"
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("select * from tasks").fetchall()
    finally:
        con.close()
    return {str(row["task_id"]): dict(row) for row in rows}


def load_benchmark_rows(paths: list[Path]) -> dict[str, list[dict[str, Any]]]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in paths:
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_source_file"] = str(path)
            row["_source_line"] = line_no
            if row.get("task_id"):
                by_task[str(row["task_id"])].append(row)
    return by_task


def stage_counts_from_progress(progress: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for stage in STAGES:
        if re.search(rf"(?:\[native\]\s+{re.escape(stage)}(?:__\w+)?\b|\[{re.escape(stage)}\])", progress):
            counts[stage] += 1
        counts[stage] += len(re.findall(rf"\[{re.escape(stage)}\]", progress))
    if "[stage] decompose starting" in progress or "[stage] decompose complete" in progress:
        counts["decompose"] += 1
    if "[stage] synthesize" in progress:
        counts["synthesize"] += len(re.findall(r"\[stage\] synthesize", progress))
    return counts


def extract_plan_signals(plan_text: str) -> dict[str, Any]:
    have_ids = re.findall(r"^\s*have\s+([A-Za-z_][A-Za-z0-9_']*)\b", plan_text, re.MULTILINE)
    lean_types = re.findall(r"lean_type:\s*'?\"?([^'\"\n]+)", plan_text)
    closing = ""
    for line in reversed(plan_text.splitlines()):
        if line.strip().startswith(("exact ", "calc")):
            closing = line.strip()
            break
    return {
        "n_have_lines": len(have_ids),
        "have_ids": have_ids,
        "n_lean_types": len(lean_types),
        "closing_line": closing,
        "has_fragile_close": "▸" in plan_text or ".trans" in plan_text,
        "has_trailing_sorry_comma": bool(re.search(r":=\s*sorry,", plan_text)),
    }


def _secondary_tags(record: dict[str, Any]) -> list[str]:
    status = str(record.get("status") or "")
    error = str(record.get("error") or "")
    context = record.get("context") or {}
    progress = record.get("progress") or ""
    skeleton_errors = " ".join(str(e) for e in context.get("skeleton_errors") or [])
    text = " ".join([status, error, skeleton_errors, progress]).lower()
    tags: list[str] = []
    if "lean verifier unavailable" in text or "timed out" in text or "verifier unavailable" in text:
        tags.append("verifier_latency")
    if "subgoal_unbound_context" in text or context.get("subgoal_unbound_context"):
        tags.append("binder-threading ceiling")
    if "skeleton_check failed" in text or "no scaffold in plan" in text:
        tags.append("decomposer scaffold contract")
    if "litellm." in text or "connection error" in text or "badrequesterror" in text:
        tags.append("operational/infra")
    if status == "running":
        tags.append("operational/infra")
    return sorted(set(tags))


def classify(record: dict[str, Any]) -> tuple[str, list[str]]:
    status = str(record.get("status") or "")
    error = str(record.get("error") or "")
    context = record.get("context") or {}
    progress = record.get("progress") or ""
    plan = record.get("plan_signals") or {}
    skeleton_errors = " ".join(str(e) for e in context.get("skeleton_errors") or [])
    text = " ".join([status, error, skeleton_errors, progress]).lower()

    reasons: list[str] = []

    bench_rows = record.get("benchmark_rows") or []
    if status == "done":
        if any((row.get("case_id") or "").startswith("core-") for row in bench_rows):
            reasons.append("curated_core_case")
            return "eval methodology / self-authoring contamination", reasons
        reasons.append("terminal_done")
        return "success", reasons

    if status == "running":
        reasons.append("db_status_running")
        if "[hyperion] status=" not in progress:
            reasons.append("no_terminal_progress_marker")
        return "operational/infra", reasons

    if "lean verifier unavailable" in text or "timed out" in text or "verifier unavailable" in text:
        reasons.append("verifier_unavailable_or_timeout")
        if context.get("skeleton_ok") is None:
            reasons.append("skeleton_ok_null")
        return "verifier_latency", reasons

    if "litellm." in text or "connection error" in text or "badrequesterror" in text:
        reasons.append("llm_or_router_infra_error")
        return "operational/infra", reasons

    if "subgoal_unbound_context" in text or context.get("subgoal_unbound_context"):
        reasons.append("subgoal_unbound_context")
        return "binder-threading ceiling", reasons

    if "skeleton_check failed" in text or "no scaffold in plan" in text:
        reasons.append("skeleton_check_failed")
        if plan.get("has_fragile_close"):
            reasons.append("fragile_closing")
        if plan.get("has_trailing_sorry_comma"):
            reasons.append("trailing_sorry_comma")
        return "decomposer scaffold contract", reasons

    if "assembled result.lean failed final verification" in text:
        reasons.append("final_verify_failed")
        return "decomposer scaffold contract", reasons

    if plan.get("closing_line", "").startswith("exact h") and plan.get("n_have_lines", 0) >= 3:
        reasons.append("multi_have_plain_exact_close")
        return "decomposer scaffold contract", reasons

    if "cannot assemble result.lean; undischarged" in text:
        reasons.append("normal_prover_stall_after_skeleton")
        return "proof sourcing / downstream stall", reasons

    reasons.append("unclassified")
    return "unclassified", reasons


def collect_task(root: Path, task_id: str, db_row: dict[str, Any], benchmark_rows: list[dict[str, Any]]) -> dict[str, Any]:
    task_dir = root / task_id
    context = read_json(task_dir / "context.json") or {}
    progress_path = task_dir / "progress.log"
    plan_path = task_dir / "plan.md"
    progress = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
    plan_text = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""
    record: dict[str, Any] = {
        "root": str(root),
        "task_id": task_id,
        "status": db_row.get("status"),
        "error": db_row.get("error"),
        "created_at": db_row.get("created_at"),
        "updated_at": db_row.get("updated_at"),
        "eval_mode": db_row.get("eval_mode") or context.get("eval_mode"),
        "lean_profile": db_row.get("lean_profile") or context.get("lean_profile"),
        "context": context,
        "progress": progress,
        "plan_signals": extract_plan_signals(plan_text),
        "benchmark_rows": benchmark_rows,
        "stage_counts": dict(stage_counts_from_progress(progress)),
    }
    bucket, reasons = classify(record)
    secondary_tags = _secondary_tags(record)
    return {
        "task_id": task_id,
        "root": str(root),
        "status": record["status"],
        "error": record["error"],
        "eval_mode": record["eval_mode"],
        "lean_profile": record["lean_profile"],
        "bucket": bucket,
        "secondary_tags": secondary_tags,
        "reasons": reasons,
        "skeleton_ok": context.get("skeleton_ok"),
        "skeleton_errors": context.get("skeleton_errors") or [],
        "subgoal_unbound_context": context.get("subgoal_unbound_context") or [],
        "final_verify": context.get("final_verify"),
        "plan_signals": record["plan_signals"],
        "stage_counts": record["stage_counts"],
        "benchmark_rows": benchmark_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-root", action="append", type=Path, default=[])
    parser.add_argument("--benchmark-result", action="append", type=Path, default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    roots = args.task_root or [Path("ai-router/tasks"), Path("agents/hyperion/tasks")]
    bench_paths = args.benchmark_result or sorted(Path("agents/hyperion/tasks").glob("benchmark-*.jsonl"))
    benchmark_rows = load_benchmark_rows(bench_paths)

    tasks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        db_rows = load_db_rows(root)
        for task_id, row in sorted(db_rows.items()):
            key = (str(root), task_id)
            seen.add(key)
            tasks.append(collect_task(root, task_id, row, benchmark_rows.get(task_id, [])))

    bucket_counts = Counter(t["bucket"] for t in tasks)
    secondary_counts = Counter(tag for t in tasks for tag in t.get("secondary_tags", []))
    stage_reach = Counter()
    for task in tasks:
        for stage, count in task["stage_counts"].items():
            if count:
                stage_reach[stage] += 1

    result = {
        "task_roots": [str(r) for r in roots],
        "benchmark_result_files": [str(p) for p in bench_paths if p.exists()],
        "n_tasks": len(tasks),
        "bucket_counts": dict(bucket_counts),
        "secondary_tag_counts": dict(secondary_counts),
        "stage_reach_task_counts": {stage: stage_reach.get(stage, 0) for stage in STAGES},
        "tasks": tasks,
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
