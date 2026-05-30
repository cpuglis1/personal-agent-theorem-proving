"""
Backward-compatibility shim. The execution engine now lives in crews/runner.py
(the unified stage-runner, PLAN_UNIFIED.md Phase 1).

``run_crew`` is preserved as an alias of ``runner.run_task`` so existing callers
(server/mcp.py) keep working. ``CapExceeded`` / ``ToolCallTracker`` are re-exported
because tests import them from this module.
"""

from __future__ import annotations

from hyperion.crews.runner import CapExceeded, ToolCallTracker, resume_task, run_task

# Old public name → new entry point. Signature is a superset (adds hitl=).
run_crew = run_task

__all__ = ["run_crew", "run_task", "resume_task", "CapExceeded", "ToolCallTracker"]
