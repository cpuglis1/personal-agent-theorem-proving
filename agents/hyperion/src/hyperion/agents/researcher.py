"""Researcher agent — web + second brain information gathering."""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import worker_llm
from hyperion.tools.second_brain import SecondBrainTool
from hyperion.tools.web_search import WebSearchTool
from hyperion.tools.workspace import WorkspaceReadTool, WorkspaceWriteTool


def make_researcher(task_id: str) -> Agent:
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
