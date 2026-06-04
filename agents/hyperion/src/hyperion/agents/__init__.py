"""Hyperion agent definitions package.

This package groups the individual CrewAI agent roles that make up the
Hyperion multi-agent orchestrator, plus the registry that wires them
together for crew/workflow construction.

Modules
-------
- ``planner``      : Decomposes a task into an ordered plan of subtasks.
- ``researcher``   : Gathers information (second_brain, web_search, etc.).
- ``developer``    : Produces concrete deliverables / code / artifacts.
- ``critic``       : Reviews and critiques upstream agent output.
- ``synthesizer``  : Merges intermediate results into a final answer.
- ``registry``     : Central lookup/factory mapping agent names to their
                     constructed CrewAI ``Agent`` instances; consumed by
                     ``hyperion.crews.runner`` and the workflow DAGs.

Design notes
------------
This file is intentionally an empty package marker (no executable code):
it only declares ``hyperion.agents`` as an importable package. Keeping it
free of imports avoids circular-import issues, since the agent modules and
the registry import shared infrastructure (LLMs, tools) that in turn may
reference back into this package. Import the concrete agents/registry from
their respective submodules rather than from this ``__init__``.
"""
