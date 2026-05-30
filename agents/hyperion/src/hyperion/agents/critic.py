"""Critic agent — optional quality-review pass on synthesizer output (Phase 2.5)."""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import planner_llm
from hyperion.tools.workspace import WorkspaceReadTool, WorkspaceWriteTool


def make_critic(task_id: str) -> Agent:
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
