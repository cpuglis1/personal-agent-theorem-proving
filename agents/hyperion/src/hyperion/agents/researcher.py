"""Researcher agent — web + second brain information gathering.

This module is the factory for Hyperion's "researcher" agent, one of the
specialist roles (planner / researcher / developer / critic / synthesizer) that
make up the multi-agent crew orchestrated by ``hyperion.crews.runner``.

Role in the system
------------------
Within a research workflow DAG the planner first emits a ``plan.md`` into the
per-run workspace. The researcher then reads that plan, fans out across its
information-gathering tools, and writes one Markdown note per subtask into the
workspace ``notes/`` directory. Downstream agents (critic, synthesizer) consume
those notes. The agent therefore communicates with the rest of the crew almost
entirely through the shared workspace filesystem rather than through return
values.

Tooling / design decisions
--------------------------
- ``SecondBrainTool`` queries Charlie's Qdrant-indexed Obsidian vault; the
  ``WebSearchTool`` hits the SearXNG instance for real-time results. Combining a
  private knowledge base with live web search lets the agent ground answers in
  both personal context and current information.
- Workspace read/write tools are constructed with ``task_id`` so every file
  access is scoped (sandboxed) to the current run's workspace directory.
- The LLM is resolved via ``worker_llm`` with ``agent_role="researcher"`` so the
  router can apply role-specific model selection and per-agent fallbacks, and so
  usage/cost is attributed to this role for the given ``task_id``.
- ``allow_delegation=False`` keeps this agent from spawning sub-agents — it is a
  leaf worker in the crew. ``max_iter=10`` bounds the tool-use loop to avoid
  runaway iteration on a single research task.
"""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import worker_llm
from hyperion.tools.second_brain import SecondBrainTool
from hyperion.tools.web_search import WebSearchTool
from hyperion.tools.workspace import WorkspaceReadTool, WorkspaceWriteTool


def make_researcher(task_id: str) -> Agent:
    """Construct the CrewAI researcher Agent for a single Hyperion run.

    The returned agent reads ``plan.md`` from the run workspace, executes each
    research subtask using web + second-brain search, and writes cited findings
    as Markdown files under ``notes/`` in the same workspace.

    Args:
        task_id: Identifier of the current Hyperion run. Threaded into the LLM
            factory (for role/cost attribution and per-run model selection) and
            into the workspace tools so all file I/O is sandboxed to this run's
            workspace directory.

    Returns:
        A configured ``crewai.Agent`` with the "Research Specialist" role,
        equipped with second-brain search, web search, and workspace
        read/write tools. Delegation is disabled and the tool-use loop is
        capped at 10 iterations.
    """
    return Agent(
        role="Research Specialist",
        goal=(
            "Read plan.md from the workspace. "
            "Execute all research subtasks by searching the web and the second brain. "
            "Write findings to notes/ in the workspace (one Markdown file per subtask). "
            "Cite sources. Do not fabricate information."
        ),
        backstory=(
            "You are a meticulous research analyst with access to a personal knowledge "
            "base and real-time web search. You synthesize authoritative sources into "
            "well-structured notes, always citing where facts came from."
        ),
        llm=worker_llm(task_id=task_id, agent_role="researcher"),
        tools=[
            SecondBrainTool(),
            WebSearchTool(),
            WorkspaceReadTool(task_id=task_id),
            WorkspaceWriteTool(task_id=task_id),
        ],
        verbose=True,
        allow_delegation=False,
        max_iter=10,
    )
