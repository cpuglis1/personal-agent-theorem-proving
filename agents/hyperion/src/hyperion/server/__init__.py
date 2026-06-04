"""Hyperion server package: HTTP / MCP / webhook entry points for the orchestrator.

This package marks the ``hyperion.server`` namespace and groups all of the
externally-facing interface layers of the Hyperion multi-agent orchestrator.
It contains no code of its own (it is an empty package initializer); the actual
endpoint implementations live in the sibling modules:

- ``api.py``         -- FastAPI HTTP API (served on :4100; see CLAUDE.md).
- ``mcp.py``         -- Model Context Protocol server exposing Hyperion as MCP tools.
- ``webhooks.py``    -- Inbound webhook handlers for external event triggers.
- ``affordances.py`` -- Affordance descriptors advertising what the agent can do.
- ``meta_tasks.py``  -- Meta/administrative task endpoints (introspection, control).

Design notes:
- This file is intentionally left without imports. Keeping the package
  initializer side-effect free avoids import-time cycles between the server
  modules and the lower layers (agents, crews, memory, tools) and lets callers
  import only the specific interface module they need.
- Per the project convention, all LLM calls made by these interfaces route
  through the LiteLLM proxy (http://localhost:4000/v1), never provider APIs
  directly.
"""
