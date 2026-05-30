"""Developer agent — code writing and execution (Phase 2.1)."""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import worker_llm


def make_developer(task_id: str) -> Agent:
    # tools/code_runner.py and workspace tools are added in Phase 2.1
    return Agent(
        role="Software Developer",
        goal=(
            "Write Python code to complete development subtasks from plan.md. "
            "Execute code in the sandbox and capture output to the workspace. "
            "Save all scripts and their outputs under artifacts/."
        ),
        backstory=(
            "You are a pragmatic software engineer who solves problems by writing "
            "clean, runnable Python. You prefer simple solutions over clever ones, "
            "and always verify code by running it before reporting results."
        ),
        llm=worker_llm(),
        tools=[],  # code_runner + workspace tools wired in Phase 2.1
        verbose=True,
        allow_delegation=False,
        max_iter=8,
    )
