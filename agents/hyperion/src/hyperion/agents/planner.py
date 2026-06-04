"""Planner agent — decomposes the task into a structured plan.

This module defines the **Planner** role in Hyperion's multi-agent crew. The
planner is typically the first agent to run in a workflow DAG: it reads the
user's raw request and produces a structured `plan.md` artifact in the run's
workspace that downstream agents (researcher, developer, critic, synthesizer)
consume to coordinate their work.

Role in the system
------------------
- Hyperion orchestrates CrewAI agents through workflow DAGs (see
  ``agents/hyperion/config/workflows/*.json`` and ``crews/runner.py``).
- Each agent is constructed via a ``make_*`` factory that wires up its role,
  goal, backstory, LLM, and tools. This file provides ``make_planner``.
- The planner's only tool is :class:`WorkspaceWriteTool`, which lets it persist
  ``plan.md`` to the per-run workspace. It deliberately has no read/search/code
  tools — its job is decomposition and routing, not execution.

Key design decisions / non-obvious context
------------------------------------------
- **Contract is the file, not the return value.** The planner communicates with
  downstream agents by writing ``plan.md`` containing YAML front-matter
  (``task_id``, ``original_request``, ``subtasks``, ``needs_review``) followed by
  a Markdown narrative. Other parts of the system parse this front-matter, so the
  goal text above is effectively an interface contract — changing it changes
  behavior for consumers.
- **LLM is task-scoped.** ``planner_llm(task_id=...)`` resolves the configured
  (and possibly per-agent fallback) model for this run via the LiteLLM proxy;
  all LLM calls route through LiteLLM by convention rather than a provider SDK.
- **Bounded autonomy.** ``allow_delegation=False`` and ``max_iter=3`` keep the
  planner from delegating to other agents or looping indefinitely — planning
  should be quick and self-contained.
"""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import planner_llm
from hyperion.tools.workspace import WorkspaceWriteTool


def make_planner(task_id: str) -> Agent:
    """Build the Planner CrewAI agent for a given run.

    Constructs an :class:`crewai.Agent` configured to decompose the user's
    request into an actionable plan and persist it as ``plan.md`` in the run's
    workspace (via :class:`WorkspaceWriteTool`).

    Args:
        task_id: Unique identifier for the current Hyperion run. Used to
            (a) resolve the task-scoped LLM through ``planner_llm`` and
            (b) scope the :class:`WorkspaceWriteTool` to this run's workspace so
            the agent writes into the correct directory.

    Returns:
        Agent: A configured planner agent. ``allow_delegation`` is disabled and
        ``max_iter`` is capped at 3 to keep planning bounded and non-recursive.

    Side effects:
        None at construction time. When later executed by a crew, the returned
        agent writes ``plan.md`` to the per-run workspace and issues LLM calls
        through the LiteLLM proxy.
    """
    return Agent(
        role="Task Planner",
        goal=(
            "Decompose the user's request into a clear, actionable plan. "
            "Write plan.md to the workspace with YAML front-matter containing:\n"
            "  task_id, original_request, subtasks (list), needs_review (bool)\n"
            "followed by a Markdown narrative of the approach. "
            "Keep plans concise — 200–400 words."
        ),
        backstory=(
            "You are a seasoned project architect who turns ambiguous goals into "
            "clear research or development plans. You know when to involve web search, "
            "when to query the second brain, and when code execution is necessary."
        ),
        llm=planner_llm(task_id=task_id),
        tools=[WorkspaceWriteTool(task_id=task_id)],
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )
