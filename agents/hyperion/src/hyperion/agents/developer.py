"""Developer agent — code writing and execution (Phase 2.1).

This module is the factory for Hyperion's "Software Developer" CrewAI agent, one
of the roles (alongside planner, researcher, critic, and synthesizer) that the
crew runner composes into a workflow DAG. The developer's job is to turn the
development subtasks laid out in ``plan.md`` into runnable Python, execute that
code in the sandbox, and persist scripts plus their outputs under the run's
``artifacts/`` directory.

Role in the system
------------------
``make_developer`` is invoked by the crew runner / agent registry
(``hyperion.crews.runner`` via ``hyperion.agents.registry``) when a workflow
includes a development step. It returns a configured :class:`crewai.Agent`; the
runner is responsible for pairing that agent with a Task and wiring it into the
DAG.

Key design decisions / non-obvious context
-----------------------------------------
- **LLM routing.** The agent's model comes from :func:`hyperion.llms.worker_llm`,
  which (per the repo-wide convention) points at the LiteLLM proxy on
  ``http://localhost:4000/v1`` rather than any provider API directly. ``worker_llm``
  is the cheaper/faster "worker" tier used for execution-heavy roles like this one.
- **Tools are intentionally empty.** ``tools=[]`` is a Phase 2.1 placeholder: the
  ``code_runner`` (sandboxed execution) and workspace tools are not yet wired in,
  so today the agent can only reason/produce code text — it cannot actually run it
  until those tools land. See the inline comments below.
- **No delegation.** ``allow_delegation=False`` keeps the developer focused on its
  own subtask; orchestration across roles is handled by the workflow DAG, not by
  agents delegating to each other.
- **``max_iter=8``** caps the agent's internal reasoning/tool-use loop to bound
  cost and latency.
"""

from __future__ import annotations

from crewai import Agent

from hyperion.llms import worker_llm


def make_developer(task_id: str) -> Agent:
    """Build the "Software Developer" CrewAI agent for a given run.

    Constructs a :class:`crewai.Agent` configured to implement development
    subtasks from ``plan.md`` by writing and (eventually) running Python in the
    sandbox, saving scripts and outputs under ``artifacts/``.

    Args:
        task_id: Identifier of the Hyperion run/task this agent belongs to.
            Accepted for a uniform agent-factory signature (every
            ``make_<role>`` factory takes ``task_id``) so the runner/registry can
            instantiate agents generically. It is not yet consumed here, but is
            the hook through which run-scoped tools (e.g. a workspace tool bound
            to this run's ``artifacts/`` directory) will be injected in Phase 2.1.

    Returns:
        crewai.Agent: A non-delegating developer agent whose LLM is the LiteLLM
        worker-tier model and whose tool list is currently empty (see module
        docstring for the Phase 2.1 tooling note).

    Side effects:
        Calls :func:`hyperion.llms.worker_llm`, which constructs an LLM client
        targeting the LiteLLM proxy. No network/I/O occurs at construction time
        beyond client setup.
    """
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
