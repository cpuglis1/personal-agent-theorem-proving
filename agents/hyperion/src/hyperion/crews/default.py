"""Backward-compatibility shim for Hyperion's crew execution entry point.

Purpose
-------
This module used to hold the crew-execution engine. That engine has since been
consolidated into ``hyperion.crews.runner`` (the unified stage-runner introduced
in Phase 1). This file is now a thin compatibility layer that
re-exports the runner's public names so historical import paths keep resolving.

Role in the system
------------------
Older callers (e.g. ``hyperion/server/mcp.py``) and the test suite import crew
primitives from ``hyperion.crews.default``. Rather than rewrite every import site
when the engine moved, this shim forwards those names to their new home. New code
should import directly from ``hyperion.crews.runner`` instead.

Re-exported names
-----------------
- ``run_crew``  : legacy public alias for the task-execution entry point.
- ``run_task``  : the current canonical entry point (from ``runner``).
- ``resume_task``: resumes a paused/interrupted task (e.g. after a HITL gate).
- ``CapExceeded``: exception raised when a usage/tool-call cap is hit.
- ``ToolCallTracker``: tracks per-run tool invocations against caps.

Design notes
-----------
- ``run_crew`` is aliased to ``run_task``. The new signature is a *superset* of
  the old one (it adds the ``hitl=`` parameter), so existing positional/keyword
  callers remain compatible without changes.
- ``CapExceeded`` and ``ToolCallTracker`` are re-exported specifically because
  tests reference them via this module's path; keep them in ``__all__``.
"""

from __future__ import annotations

# Import the canonical implementations from the unified stage-runner. Everything
# below is a forwarding re-export; no execution logic lives in this module.
from hyperion.crews.runner import CapExceeded, ToolCallTracker, resume_task, run_task

# Old public name → new entry point. Signature is a superset (adds hitl=).
run_crew = run_task

# Pin the public surface so the legacy import paths (and tests) resolve cleanly.
__all__ = ["run_crew", "run_task", "resume_task", "CapExceeded", "ToolCallTracker"]
