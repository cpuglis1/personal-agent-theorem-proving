"""Planner agent — decomposes the task into a structured plan."""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import planner_llm
from hyperion.tools.workspace import WorkspaceWriteTool


def make_planner(task_id: str) -> Agent:
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
