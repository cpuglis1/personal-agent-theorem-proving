"""Synthesizer agent — reads plan + notes, produces the final artifact.

Role in the system
------------------
This module is the factory for Hyperion's "synthesizer" agent, the final stage
in a research workflow DAG. After upstream agents (planner, researcher, critic)
have populated a per-task workspace with ``plan.md`` and a collection of
``notes/`` files, the synthesizer reads that raw material and produces the
polished deliverable at ``artifacts/result.md``.

It is a thin CrewAI ``Agent`` factory: all behavior is driven by the
role/goal/backstory prompt and the workspace tools wired in below. The agent
itself does no I/O directly — it acts on the workspace exclusively through the
``Workspace*`` tools, which are scoped to a single ``task_id`` so concurrent
runs cannot read or write each other's files.

Key design decisions / non-obvious context
------------------------------------------
- The LLM is obtained via ``worker_llm(...)`` rather than constructed inline.
  That helper resolves the model (with per-agent fallback) and routes the call
  through the LiteLLM proxy, per the repo-wide convention of never calling
  provider APIs directly.
- ``agent_role="synthesizer"`` is passed to ``worker_llm`` so model selection,
  fallback config, and usage/cost tracking can be attributed to this role.
- ``allow_delegation=False``: the synthesizer is a leaf agent and must not spawn
  or hand off work to other agents — it only writes the final report.
- ``max_iter=5`` bounds the agent's tool-use loop (list/read/write cycles) to
  avoid runaway iterations while still allowing it to read multiple note files
  before writing.
"""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import worker_llm
from hyperion.tools.workspace import WorkspaceListTool, WorkspaceReadTool, WorkspaceWriteTool


def make_synthesizer(task_id: str) -> Agent:
    """Build the synthesizer ``Agent`` for a single Hyperion task.

    Constructs a CrewAI ``Agent`` configured to read the planning and research
    artifacts from a task's workspace and write the final Markdown report. The
    returned agent is stateless beyond its configuration; callers attach it to a
    crew/task that drives the actual execution.

    Args:
        task_id: Identifier of the run whose workspace this agent operates on.
            It is threaded into both ``worker_llm`` (for per-role model
            selection and usage attribution) and each ``Workspace*`` tool (to
            scope all file access to this task's isolated workspace).

    Returns:
        A configured CrewAI ``Agent`` with the workspace list/read/write tools,
        delegation disabled, and a bounded tool-use loop (``max_iter=5``).

    Side effects:
        None directly. The agent only performs I/O when later executed as part
        of a crew, and even then exclusively via its scoped workspace tools.
    """
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
