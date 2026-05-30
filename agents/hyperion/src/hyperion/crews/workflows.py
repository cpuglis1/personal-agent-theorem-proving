"""
Workflow registry — free-form, per-agent DAG definitions.

A workflow is a named directed-acyclic graph of *nodes*; each node references an
agent (by id) and lists its upstream node ids. The runner topo-sorts the nodes and
executes them in dependency order, so a workflow can be any number of steps and an
agent can appear in more than one node (node id is distinct from agent id).

Workflows are JSON records under ``config/workflows/<id>.json`` (git-tracked,
volume-mounted like agents). They supersede the old fixed plan→work→synthesize
pipeline; the seeded ``research-default`` reproduces it exactly so behavior is
unchanged when no workflow is chosen.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import BaseModel, Field

from hyperion.config import settings

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class WorkflowNode(BaseModel):
    id: str                                   # node slug, unique within the workflow
    agent: str                                # agent record id this node runs
    upstream: list[str] = Field(default_factory=list)  # node ids that must finish first
    gate_before: bool = False                 # pause for human approval before this node (HITL)
    instruction: Optional[str] = None         # overrides the stage-derived task description


class WorkflowRecord(BaseModel):
    id: str
    name: str
    description: str = ""
    nodes: list[WorkflowNode] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _workflows_dir():
    return settings.config_dir / "workflows"


def _record_path(workflow_id: str):
    if not _SLUG_RE.match(workflow_id):
        raise ValueError(f"Invalid workflow id {workflow_id!r} (must be a slug)")
    return _workflows_dir() / f"{workflow_id}.json"


def load_workflow(workflow_id: str) -> WorkflowRecord:
    path = _record_path(workflow_id)
    if not path.exists():
        raise FileNotFoundError(f"No workflow record for id {workflow_id!r} at {path}")
    return WorkflowRecord.model_validate_json(path.read_text(encoding="utf-8"))


def load_all_workflows() -> list[WorkflowRecord]:
    d = _workflows_dir()
    if not d.exists():
        return []
    return [
        WorkflowRecord.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(d.glob("*.json"))
    ]


def save_workflow(record: WorkflowRecord) -> None:
    d = _workflows_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _record_path(record.id)
    path.write_text(
        json.dumps(record.model_dump(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def delete_workflow(workflow_id: str) -> None:
    path = _record_path(workflow_id)
    if path.exists():
        path.unlink()


def get_default_workflow() -> WorkflowRecord:
    """The workflow used when a task does not name one.

    Resolution order: the persisted ``settings.default_workflow`` id, else the
    seeded ``research-default``, else the first workflow on disk. Raises if the
    config dir has no workflows at all (a broken install)."""
    wanted = settings.default_workflow
    try:
        return load_workflow(wanted)
    except (FileNotFoundError, ValueError):
        pass
    try:
        return load_workflow("research-default")
    except (FileNotFoundError, ValueError):
        pass
    all_wf = load_all_workflows()
    if not all_wf:
        raise FileNotFoundError(
            f"No workflows found in {_workflows_dir()} (expected at least research-default.json)"
        )
    return all_wf[0]


def resolve_workflow(workflow_id: Optional[str]) -> WorkflowRecord:
    """A named workflow, or the default when ``workflow_id`` is None/empty."""
    if workflow_id:
        return load_workflow(workflow_id)
    return get_default_workflow()


# ---------------------------------------------------------------------------
# Validation + topo-sort
# ---------------------------------------------------------------------------


def topo_sort(nodes: list[WorkflowNode]) -> list[WorkflowNode]:
    """Order nodes so every node follows its upstream deps. Tie-broken by id for
    determinism. Raises ValueError on a cycle or a dangling upstream reference."""
    by_id = {n.id: n for n in nodes}
    indeg: dict[str, int] = {n.id: 0 for n in nodes}
    for n in nodes:
        for u in n.upstream:
            if u not in by_id:
                raise ValueError(f"Node {n.id!r} lists unknown upstream {u!r}")
            indeg[n.id] += 1

    ready = sorted([nid for nid, d in indeg.items() if d == 0])
    out: list[str] = []
    while ready:
        nid = ready.pop(0)
        out.append(nid)
        for other in nodes:
            if nid in other.upstream:
                indeg[other.id] -= 1
                if indeg[other.id] == 0:
                    ready.append(other.id)
        ready.sort()
    if len(out) != len(nodes):
        remaining = sorted(set(by_id) - set(out))
        raise ValueError(f"Cycle detected in workflow upstream edges among {remaining}")
    return [by_id[nid] for nid in out]


def validate_workflow(record: WorkflowRecord, known_agent_ids: set[str]) -> None:
    """Structural validation: slug id, >=1 node, unique node ids, every node
    references a known agent, upstream refs resolve within the workflow, acyclic."""
    if not _SLUG_RE.match(record.id):
        raise ValueError(f"Workflow id {record.id!r} must be a slug ([a-z0-9_-])")
    if not record.nodes:
        raise ValueError("A workflow must have at least one node.")

    seen: set[str] = set()
    for node in record.nodes:
        if not _SLUG_RE.match(node.id):
            raise ValueError(f"Node id {node.id!r} must be a slug ([a-z0-9_-])")
        if node.id in seen:
            raise ValueError(f"Duplicate node id {node.id!r} in workflow {record.id!r}")
        seen.add(node.id)
        if node.agent not in known_agent_ids:
            raise ValueError(
                f"Node {node.id!r} references unknown agent {node.agent!r}"
            )
    for node in record.nodes:
        for u in node.upstream:
            if u not in seen:
                raise ValueError(
                    f"Node {node.id!r} lists upstream {u!r} which is not a node in this workflow"
                )
    # Raises on a cycle.
    topo_sort(record.nodes)
