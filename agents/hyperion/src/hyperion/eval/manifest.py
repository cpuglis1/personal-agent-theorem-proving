"""Evaluation manifest helpers.

The benchmark harness needs reproducible run metadata before any theorem is attempted:
code/config versions, eval mode, verifier profile, and hashes for the prompt/config
surface. This module writes that snapshot into the task artifacts directory. It is
best-effort and never participates in proof acceptance.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from hyperion.config import settings


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _hash_tree(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    try:
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            rel = item.relative_to(path).as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(item.read_bytes())
            h.update(b"\0")
    except OSError:
        return None
    return h.hexdigest()


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parents[5],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _git_dirty() -> bool | None:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).parents[5],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except Exception:
        return None


def run_manifest(
    *,
    task_id: str,
    request: str,
    workflow: str | None,
    eval_mode: str,
    lean_profile: str,
    caps: dict[str, Any],
    problem_id: str | None = None,
    split: str | None = None,
    order_seed: int | None = None,
) -> dict[str, Any]:
    """Build the reproducibility manifest for one run."""
    config_dir = settings.config_dir
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "problem_id": problem_id,
        "split": split,
        "order_seed": order_seed,
        "eval_mode": eval_mode,
        "learning_writes_enabled": eval_mode == "train",
        "workflow": workflow or settings.default_workflow,
        "request_sha256": _sha256_bytes(request.encode("utf-8")),
        "git": {"commit": _git_commit(), "dirty": _git_dirty()},
        "models": {
            "planner": settings.model_planner,
            "worker": settings.model_worker,
            "cheap": settings.model_cheap,
        },
        "verifier": {
            "lean_url": settings.lean_url,
            "lean_profile": lean_profile,
            "lean_toolchain_sha256": _hash_file(Path(__file__).parents[3] / "lean-sidecar" / "lean-toolchain"),
            "lakefile_sha256": _hash_file(Path(__file__).parents[3] / "lean-sidecar" / "lakefile.lean"),
        },
        "retrieval": {
            "mode": settings.lemma_retrieval_mode,
            "qdrant_url": settings.qdrant_url,
            "skill_library_collection": settings.qdrant_skill_library_collection,
            "mathlib_premises_collection": settings.qdrant_mathlib_premises_collection,
            "concepts_collection": settings.qdrant_concepts_collection,
            "retrieval_config_hash": _sha256_bytes(
                json.dumps(
                    {
                        "mode": settings.lemma_retrieval_mode,
                        "skill": settings.qdrant_skill_library_collection,
                        "mathlib": settings.qdrant_mathlib_premises_collection,
                        "concepts": settings.qdrant_concepts_collection,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ),
        },
        "config_hashes": {
            "agents": _hash_tree(config_dir / "agents"),
            "workflows": _hash_tree(config_dir / "workflows"),
            "models": _hash_file(config_dir / "models.json"),
            "model_registry": _hash_file(config_dir / "model_registry.json"),
            "thresholds": _hash_file(config_dir / "thresholds.json"),
        },
        "caps": caps,
    }


def write_run_manifest(
    *,
    task_id: str,
    request: str,
    workflow: str | None,
    eval_mode: str,
    lean_profile: str,
    caps: dict[str, Any],
    problem_id: str | None = None,
    split: str | None = None,
    order_seed: int | None = None,
) -> Path | None:
    """Write ``artifacts/eval_manifest.json`` for a task, best-effort."""
    try:
        manifest = run_manifest(
            task_id=task_id,
            request=request,
            workflow=workflow,
            eval_mode=eval_mode,
            lean_profile=lean_profile,
            caps=caps,
            problem_id=problem_id,
            split=split,
            order_seed=order_seed,
        )
        path = settings.tasks_dir / task_id / "artifacts" / "eval_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return path
    except Exception:
        return None
