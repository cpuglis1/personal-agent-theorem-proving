"""Critic agent — optional quality-review pass on synthesizer output (Phase 2.5).

Role in the system
------------------
This module defines the "Quality Critic" agent, one of Hyperion's CrewAI-based
agents (alongside planner, researcher, developer, and synthesizer). The critic
sits at the tail end of a workflow DAG, after the synthesizer has produced the
final report. Its job is a self-review / quality-gate pass: it reads the
synthesized report and either approves it or emits an actionable critique that a
downstream revision step (or a re-run of the synthesizer) can act on.

How it fits the workflow
------------------------
- Input contract:  reads ``artifacts/result.md`` from the run's workspace
  (the synthesizer's output).
- Output contract: writes ``artifacts/critique.md`` to the same workspace.
  The sentinel string ``"APPROVED"`` signals "no revision needed"; any other
  content is a structured, actionable critique. Downstream steps and the
  workflow runner branch on this sentinel.

Both files live in the per-run workspace keyed by ``task_id`` and are reached
exclusively through the sandboxed Workspace tools (no direct filesystem access).

Design decisions / non-obvious context
-------------------------------------
- This is an *optional* pass: workflows that don't need a critique-revision
  loop simply omit this agent from their DAG (e.g. ``research-default.json``
  vs. ``research-critique.json``).
- The critic reuses ``planner_llm()`` rather than a dedicated model — quality
  evaluation benefits from the same higher-capability model used for planning.
  All LLM traffic still routes through the LiteLLM proxy per repo convention.
- ``allow_delegation=False`` keeps the critic from spawning sub-agents; it is a
  single-responsibility reviewer.
- ``max_iter=3`` bounds the reason/act loop (read result -> evaluate -> write
  critique) so a confused agent cannot loop indefinitely on the workspace tools.
"""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import planner_llm
from hyperion.tools.workspace import WorkspaceReadTool, WorkspaceWriteTool


def make_critic(task_id: str) -> Agent:
    """Build the Quality Critic CrewAI agent for a given run.

    Constructs an :class:`crewai.Agent` configured to review the synthesized
    report for a single Hyperion run. The agent's ``goal`` encodes its full
    behavioral contract: read ``artifacts/result.md``, evaluate it on factual
    accuracy, completeness, structure, and clarity, then write either a
    structured critique or the sentinel ``"APPROVED"`` to
    ``artifacts/critique.md``.

    The agent is given workspace tools scoped to ``task_id`` so all reads and
    writes are confined to that run's sandboxed workspace.

    Args:
        task_id: Identifier for the current run. Used to scope the
            :class:`WorkspaceReadTool` and :class:`WorkspaceWriteTool` to this
            run's workspace directory; it does not appear in the agent's prompt.

    Returns:
        A configured :class:`crewai.Agent` ready to be attached to a CrewAI
        task in the workflow runner. The agent is not executed here — execution
        is driven by the crew/runner once a task is assigned to it.

    Side effects:
        None at construction time. The returned agent, when run, reads and
        writes files in the run workspace via its Workspace tools and issues
        LLM calls through the LiteLLM proxy (via ``planner_llm()``).
    """
    return Agent(
        role="Quality Critic",
        goal=(
            "Read artifacts/result.md. "
            "Evaluate quality on: factual accuracy, completeness, structure, and clarity. "
            "If the report needs revision, write a structured critique to artifacts/critique.md "
            "with specific, actionable improvements. "
            "If the report is satisfactory, write 'APPROVED' to artifacts/critique.md and stop."
        ),
        backstory=(
            "You are a rigorous editor and fact-checker. You hold work to a high standard "
            "and provide specific, constructive feedback. You only approve reports that "
            "are accurate, complete, and clearly written."
        ),
        llm=planner_llm(),
        tools=[
            WorkspaceReadTool(task_id=task_id),
            WorkspaceWriteTool(task_id=task_id),
        ],
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )
