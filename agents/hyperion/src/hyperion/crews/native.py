"""
Native node handlers — the deterministic, non-agent DAG citizens.

A workflow node with ``kind == "native"`` (see ``hyperion.crews.workflows``) runs a
registered plain-Python handler instead of a CrewAI agent or a child workflow. This
is the seam the Lean prover uses for its deterministic steps — ``retrieve``,
``verify`` (a controller that *calls* an agent but owns its own loop/routing),
``compare``, and ``bank`` — which are control-flow-deterministic and would only be
slowed down and obscured by wrapping them in a ReAct agent.

Design (mirrors ``agents/registry.py``'s ``TOOL_REGISTRY``)
-----------------------------------------------------------
- ``NATIVE_HANDLERS`` is a name -> async-handler registry; ``register_native_handler``
  adds entries (later phases register ``retrieve``/``verify``/``compare``/``bank``).
- A handler receives a small typed :class:`NativeNodeCtx` (task id, the node, the
  run request, blackboard accessors, a progress sink) and returns a result dict that
  the runner records exactly like a stage output.
- ``run_native_node`` is the single dispatch entry the runner calls from ``_run_one``,
  exactly parallel to how a subworkflow node dispatches to ``_run_subworkflow``.

Native nodes run inside the same ``try`` in ``runner._execute_workflow`` as every
other node, so they inherit the same ``CapExceeded`` / wall-budget protection.

Phase 1 ships only a trivial ``echo`` handler so the seam is testable now; the real
handlers land in Phases 3–5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from hyperion.crews.workflows import WorkflowNode
from hyperion.memory.context_store import context_get, context_put

logger = logging.getLogger(__name__)

# A native handler's return value: an arbitrary result dict the runner records like
# a stage output (e.g. ``{"handler": ..., "ok": ..., ...}``).
NativeResult = dict[str, Any]


@dataclass
class NativeNodeCtx:
    """The small typed context handed to a native handler.

    Carries everything a deterministic step needs without exposing the whole runner
    loop: the bound ``task_id`` (for workspace/blackboard scoping), the node itself
    (``handler``/``instruction``/``id``), the run request, and a progress sink.
    Blackboard reads/writes go through :meth:`get` / :meth:`put` (the shared
    per-task ``context.json``), so handlers never touch the store directly.

    Attributes:
        task_id: The current run/task id; scopes the blackboard and workspace.
        node: The native ``WorkflowNode`` being executed.
        request: The run's request string (the target theorem, for the prover).
        progress_callback: Optional progress sink; use :meth:`progress`.
    """

    task_id: str
    node: WorkflowNode
    request: str
    progress_callback: Optional[Callable[[str], None]] = None

    def get(self, key: str, default: Any = None) -> Any:
        """Read ``key`` from the task blackboard, or ``default`` when absent/None."""
        value = context_get(self.task_id, key)
        return default if value is None else value

    def put(self, key: str, value: Any) -> None:
        """Write ``key`` to the task blackboard so later nodes can read it."""
        context_put(self.task_id, key, value)

    def progress(self, message: str) -> None:
        """Emit a progress message if a sink is wired (no-op otherwise)."""
        if self.progress_callback:
            self.progress_callback(message)


# A native handler is an async callable from a context to a result dict. Async so it
# can ``await`` the same things agent stages do (LLM calls in the verify controller,
# I/O to the Lean sidecar / Qdrant) without blocking the runner's event loop.
NativeHandler = Callable[[NativeNodeCtx], Awaitable[NativeResult]]

# name -> handler. Mirrors TOOL_REGISTRY. Populated by register_native_handler.
NATIVE_HANDLERS: dict[str, NativeHandler] = {}


def register_native_handler(name: str, handler: NativeHandler) -> None:
    """Register a native handler under ``name``.

    Later phases call this to add ``retrieve``/``verify``/``compare``/``bank``.

    Args:
        name: Registry key referenced by a native node's ``handler`` field.
        handler: The async handler callable.

    Side effects:
        Mutates the module-global ``NATIVE_HANDLERS`` in place. Re-registering an
        existing name overwrites the previous handler.
    """
    NATIVE_HANDLERS[name] = handler


def get_native_handler(name: str) -> NativeHandler:
    """Resolve a handler by name.

    Args:
        name: A registry key.

    Returns:
        The registered handler.

    Raises:
        ValueError: If ``name`` is not present in ``NATIVE_HANDLERS``.
    """
    handler = NATIVE_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown native handler {name!r} (not in NATIVE_HANDLERS)")
    return handler


async def run_native_node(ctx: NativeNodeCtx) -> NativeResult:
    """Dispatch a native node to its registered handler.

    The single entry the runner calls for a ``kind == "native"`` node (parallel to
    ``_run_subworkflow`` for subworkflow nodes). The handler key comes from
    ``ctx.node.handler``.

    Args:
        ctx: The native node context (carries the node, task id, request, sink).

    Returns:
        The handler's result dict.

    Raises:
        ValueError: If ``ctx.node.handler`` is unset or not registered.
    """
    if not ctx.node.handler:
        raise ValueError(f"Native node {ctx.node.id!r} has no 'handler' set")
    handler = get_native_handler(ctx.node.handler)
    ctx.progress(f"[native] {ctx.node.id} → {ctx.node.handler}")
    return await handler(ctx)


# ---------------------------------------------------------------------------
# Phase 1: a trivial echo handler so the seam is exercisable end-to-end now.
# Real handlers (retrieve/verify/compare/bank) register in Phases 3–5.
# ---------------------------------------------------------------------------


async def _echo_handler(ctx: NativeNodeCtx) -> NativeResult:
    """Trivial handler: echo the node's instruction back and record it.

    Writes the echoed payload to the blackboard (proving the write path works) and
    returns a result dict the runner records. Used by the Phase 1 native-node gate.
    """
    payload = ctx.node.instruction or ""
    ctx.put(f"native_echo_{ctx.node.id}", payload)
    return {
        "handler": "echo",
        "node": ctx.node.id,
        "request": ctx.request,
        "echo": payload,
    }


register_native_handler("echo", _echo_handler)


# ---------------------------------------------------------------------------
# Prover handlers (Phase 4): import for side-effect registration of the
# retrieve/skeleton_check/verify/bank handlers. Done at the bottom so the
# registry primitives above are defined first and the import can't be circular
# (lean_handlers imports only register_native_handler + NativeNodeCtx from here).
# ---------------------------------------------------------------------------
from hyperion.crews import lean_handlers as _lean_handlers  # noqa: E402,F401
