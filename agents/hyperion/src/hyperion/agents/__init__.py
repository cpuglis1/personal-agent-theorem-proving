"""Hyperion agent definitions package.

Agents are pure, data-driven personas: each is a JSON record under
``config/agents/<id>.json`` (role/goal/backstory + model/tool config). The runner
builds an executor from a record via ``registry.load_agent`` +
``crews.runner.build_agent`` — there are no hand-coded per-role agent factories
(the old CrewAI ``planner``/``researcher``/``developer``/``critic``/``synthesizer``
modules were removed in Phase 2 when CrewAI was dropped).

Modules
-------
- ``registry`` : Agent record schema + persistence, and the tool registry that maps
                 tool names to ``ToolSpec`` descriptors consumed by the owned agent
                 loop (``hyperion.agent_loop``).

Design notes
------------
This file is intentionally an empty package marker (no executable code): it only
declares ``hyperion.agents`` as an importable package. Keeping it import-free
avoids circular-import issues. Import ``registry`` from its submodule directly.
"""
