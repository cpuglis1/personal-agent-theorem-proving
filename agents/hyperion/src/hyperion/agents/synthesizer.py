"""Synthesizer agent — reads plan + notes, produces the final artifact."""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import worker_llm
from hyperion.tools.workspace import WorkspaceListTool, WorkspaceReadTool, WorkspaceWriteTool


def make_synthesizer(task_id: str) -> Agent:
    return Agent(
        role="Report Synthesizer",
        goal=(
            "Read plan.md and all files under notes/ from the workspace. "
            "Write a polished, well-structured Markdown report to artifacts/result.md. "
            "The report must directly answer the original request stated in plan.md. "
            "Include a Sources section at the end with all URLs cited."
        ),
        backstory=(
            "You are a professional writer who transforms raw research notes into "
            "clear, insightful reports. You organize information logically, write in "
            "plain language, and always ground conclusions in the provided evidence."
        ),
        llm=worker_llm(task_id=task_id, agent_role="synthesizer"),
        tools=[
            WorkspaceListTool(task_id=task_id),
            WorkspaceReadTool(task_id=task_id),
            WorkspaceWriteTool(task_id=task_id),
        ],
        verbose=True,
        allow_delegation=False,
        max_iter=5,
    )
