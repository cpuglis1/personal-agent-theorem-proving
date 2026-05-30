"""
Agent + tool registry — the data-driven substrate (Phase 0).

Agents are JSON records under ``config/agents/<id>.json`` (git-tracked, volume-mounted
so UI edits survive container rebuilds). Tools are a named registry; agent records
reference tools by name and the runner resolves them per-task.

Every later-phase capability plugs in here as a registry entry or a record edit —
never as a code edit to a hardcoded agent factory. See PLAN_UNIFIED.md §4.1/§4.6.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Literal, Optional

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from hyperion.config import settings
from hyperion.feedback import AskUserTool, ReadHumanFeedbackTool
from hyperion.memory.context_store import ContextGetTool, ContextPutTool, RecallSimilarTasksTool
from hyperion.tools.notion import NotionWriteTool
from hyperion.tools.second_brain import SecondBrainTool
from hyperion.tools.web_search import WebSearchTool
from hyperion.tools.workspace import WorkspaceListTool, WorkspaceReadTool, WorkspaceWriteTool

Stage = Literal["plan", "work", "synthesize"]
TriggerType = Literal["always", "keyword", "task_type", "upstream", "schedule"]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# Tool registry — name -> factory(task_id) -> BaseTool
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Callable[[str], BaseTool]] = {
    "workspace_read": lambda task_id: WorkspaceReadTool(task_id=task_id),
    "workspace_write": lambda task_id: WorkspaceWriteTool(task_id=task_id),
    "workspace_list": lambda task_id: WorkspaceListTool(task_id=task_id),
    "web_search": lambda task_id: WebSearchTool(),
    "second_brain": lambda task_id: SecondBrainTool(),
    "context_put": lambda task_id: ContextPutTool(task_id=task_id),
    "context_get": lambda task_id: ContextGetTool(task_id=task_id),
    "recall_similar_tasks": lambda task_id: RecallSimilarTasksTool(task_id=task_id),
    "read_human_feedback": lambda task_id: ReadHumanFeedbackTool(task_id=task_id),
    "ask_user": lambda task_id: AskUserTool(task_id=task_id),
    "notion_write": lambda task_id: NotionWriteTool(),
}


def register_tool(name: str, factory: Callable[[str], BaseTool]) -> None:
    """Register a tool factory. Later phases call this to add new capabilities."""
    TOOL_REGISTRY[name] = factory


def build_tools(names: list[str], task_id: str) -> list[BaseTool]:
    """Resolve a list of tool names against the registry for a given task."""
    tools: list[BaseTool] = []
    for name in names:
        factory = TOOL_REGISTRY.get(name)
        if factory is None:
            raise ValueError(f"Unknown tool {name!r} (not in TOOL_REGISTRY)")
        tools.append(factory(task_id))
    return tools


# ---------------------------------------------------------------------------
# Agent record schema (PLAN_UNIFIED.md §4.1)
# ---------------------------------------------------------------------------


class Trigger(BaseModel):
    """When a work-stage agent activates. Fields beyond ``type`` are filled in Phase 2."""

    type: TriggerType = "always"
    keywords: list[str] = Field(default_factory=list)
    task_types: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    cron: Optional[str] = None


class Thresholds(BaseModel):
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    max_activations_per_day: Optional[int] = None


class AgentRecord(BaseModel):
    id: str
    name: str
    description: str = ""
    group: str = "core"
    active: bool = True
    stage: Stage = "work"
    role: str
    goal: str
    backstory: str
    model_alias: str = "worker"
    fallback_alias: Optional[str] = None
    temperature: float = 0.1
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_iter: int = 3
    tools: list[str] = Field(default_factory=list)
    trigger: Trigger = Field(default_factory=Trigger)
    order: int = 0
    thresholds: Thresholds = Field(default_factory=Thresholds)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _agents_dir():
    d = settings.config_dir / "agents"
    return d


def _record_path(agent_id: str):
    if not _SLUG_RE.match(agent_id):
        raise ValueError(f"Invalid agent id {agent_id!r} (must be a slug)")
    return _agents_dir() / f"{agent_id}.json"


def load_agent(agent_id: str) -> AgentRecord:
    path = _record_path(agent_id)
    if not path.exists():
        raise FileNotFoundError(f"No agent record for id {agent_id!r} at {path}")
    return AgentRecord.model_validate_json(path.read_text(encoding="utf-8"))


def load_all_agents() -> list[AgentRecord]:
    """All records, sorted by (stage order, then ``order``, then id)."""
    d = _agents_dir()
    if not d.exists():
        return []
    records = [
        AgentRecord.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(d.glob("*.json"))
    ]
    stage_rank = {"plan": 0, "work": 1, "synthesize": 2}
    records.sort(key=lambda r: (stage_rank.get(r.stage, 1), r.order, r.id))
    return records


def save_agent(record: AgentRecord) -> None:
    d = _agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _record_path(record.id)
    path.write_text(
        json.dumps(record.model_dump(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def delete_agent(agent_id: str) -> None:
    path = _record_path(agent_id)
    if path.exists():
        path.unlink()


def validate_agent(record: AgentRecord) -> None:
    """Structural validation of a single record. Cross-record invariants
    (acyclic DAG, at-least-one-plan/synthesize) live in ``validate_collection``."""
    if not _SLUG_RE.match(record.id):
        raise ValueError(f"Agent id {record.id!r} must be a slug ([a-z0-9_-])")
    if record.stage not in ("plan", "work", "synthesize"):
        raise ValueError(f"Invalid stage {record.stage!r}")
    for tool_name in record.tools:
        if tool_name not in TOOL_REGISTRY:
            raise ValueError(f"Agent {record.id!r} references unknown tool {tool_name!r}")


# Recognized LiteLLM role aliases (multi-provider groups). A model_alias is valid
# if it is one of these or a concrete model id the proxy reports.
MODEL_ALIASES: tuple[str, ...] = ("smart", "worker", "cheap", "fast")


def _assert_acyclic(records: list[AgentRecord]) -> None:
    """Reject cycles in work-stage ``upstream`` edges (DFS 3-color)."""
    work = {r.id: r for r in records if r.stage == "work"}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rid: WHITE for rid in work}

    def visit(rid: str, path: list[str]) -> None:
        color[rid] = GRAY
        for u in work[rid].trigger.upstream:
            if u not in work:  # edge to a non-work or missing agent — not a cycle
                continue
            if color[u] == GRAY:
                cycle = " -> ".join(path + [rid, u])
                raise ValueError(f"Cycle in work-stage upstream edges: {cycle}")
            if color[u] == WHITE:
                visit(u, path + [rid])
        color[rid] = BLACK

    for rid in work:
        if color[rid] == WHITE:
            visit(rid, [])


def validate_collection(records: list[AgentRecord]) -> None:
    """Whole-store invariants that must hold after any CRUD mutation:
    at least one active plan AND one active synthesize agent, and an acyclic work DAG."""
    if not any(r.stage == "plan" and r.active for r in records):
        raise ValueError("At least one active 'plan' agent is required.")
    if not any(r.stage == "synthesize" and r.active for r in records):
        raise ValueError("At least one active 'synthesize' agent is required.")
    _assert_acyclic(records)
