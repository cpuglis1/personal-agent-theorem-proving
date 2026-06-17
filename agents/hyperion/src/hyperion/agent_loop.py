"""
agent_loop.py — the owned per-node tool-calling loop (Phase 2).

Replaces CrewAI's per-node executor. Each workflow node runs as a single agent
with a system prompt, a user task, and a set of tools; this module drives the
native LiteLLM function-calling loop that lets the agent call those tools and
return a final answer. The multi-agent orchestration CrewAI used to provide
(waves, gating, ``when``, subworkflows, revise loops, edge-threaded context) all
lives in ``hyperion.crews.runner`` — what remained of CrewAI was essentially this
ReAct loop, which Hyperion now owns outright.

Components:
  - ``ToolSpec``     — an owned tool descriptor (name/description/JSON-schema/fn),
                       replacing ``crewai.tools.BaseTool``.
  - ``Agent``        — a lightweight config dataclass (system prompt + llm + tools
                       + iteration cap); no behavior, just what a node needs to run.
  - ``AgentResult``  — the loop's output (``.raw`` mirrors CrewOutput.raw so the
                       runner consumes it unchanged).
  - ``run_agent_loop`` — the native function-calling loop over ``LlmHandle.complete``.

Parity preserved from the CrewAI path: per-node model selection, Langfuse
attribution + usage accounting + spend caps (enforced in ``LlmHandle.complete``),
the single fallback retry, progress callbacks, and the stuck-loop guard
(``ToolCallTracker``), which now rides this loop instead of CrewAI's step callback.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    """An owned, LLM-callable tool descriptor (replaces ``crewai.tools.BaseTool``).

    Attributes:
        name: Tool name exposed to the model and used to dispatch a tool call.
        description: Natural-language guidance shown to the model.
        parameters: JSON-schema object describing the tool's arguments. Property
            names must match ``fn``'s keyword parameters — the loop calls
            ``fn(**args)`` with the model-supplied arguments.
        fn: The task-scoped callable that performs the work and returns a string.
    """

    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]

    @property
    def schema(self) -> dict:
        """The OpenAI/LiteLLM function-tool schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Agent:
    """A node's executor config — pure data, no behavior (replaces ``crewai.Agent``).

    Attributes:
        system: The assembled system prompt (role + backstory + goal).
        llm: An ``LlmHandle`` bound to this node's model/caps/trace metadata.
        tools: The tools this agent may call, as ``ToolSpec`` descriptors.
        max_iter: Maximum tool-calling rounds before the loop forces a final answer.
        role: The agent's role label (kept for progress/trace readability).
    """

    system: str
    llm: Any                      # hyperion.llms.LlmHandle (avoid an import cycle)
    tools: list[ToolSpec] = field(default_factory=list)
    max_iter: int = 6
    role: str = "agent"


@dataclass
class AgentResult:
    """The loop's output. ``raw`` mirrors CrewAI's ``CrewOutput.raw`` so the runner
    (``_output_text`` / ``_write_fallback_result``) consumes it without changes."""

    raw: str


def _message_to_dict(msg: Any) -> dict:
    """Normalize a LiteLLM response message into a conversation dict with any
    tool calls preserved, so it can be appended to ``messages`` for the next turn."""
    tool_calls = getattr(msg, "tool_calls", None) or []
    out: dict[str, Any] = {"role": "assistant", "content": getattr(msg, "content", None) or ""}
    if tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in tool_calls
        ]
    return out


def _parse_args(raw_args: str) -> dict:
    """Parse a tool call's JSON arguments, tolerating an empty/malformed payload."""
    if not raw_args:
        return {}
    try:
        parsed = json.loads(raw_args)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def run_agent_loop(
    *,
    system: str,
    user: str,
    tools: list[ToolSpec],
    llm: Any,
    max_iter: int,
    step_cb: Callable[[str], None] | None = None,
    task_cb: Callable[[str], None] | None = None,
    tracker: Any = None,
    deadline: float | None = None,
) -> AgentResult:
    """Run a node as a native LiteLLM function-calling loop.

    Steps:
        1. Seed the conversation with the system + user messages.
        2. Up to ``max_iter`` rounds: ask the model (offering the tool schemas);
           if it returns tool calls, execute each and append the results, then
           loop; otherwise return its content as the answer.
        3. If the iteration budget is exhausted, do one final tool-less completion
           to force a written answer.

    Caps/fallback are enforced inside ``llm.complete``. The ``tracker`` (the
    stuck-loop ``ToolCallTracker``) is fed before each tool runs and may raise
    ``CapExceeded`` to abort a wedged ReAct loop — that propagates to the runner.

    Args:
        system: System prompt for the agent.
        user: The node's task (instruction + edge-threaded upstream context).
        tools: Tools the agent may call.
        llm: An ``LlmHandle`` exposing ``complete(messages, tools=...)``.
        max_iter: Maximum tool-calling rounds.
        step_cb: Optional progress sink called with a short label per step.
        task_cb: Optional progress sink called once when the node's answer is ready.
        tracker: Optional ``ToolCallTracker`` for the stuck-loop guard.
        deadline: Optional ``time.monotonic()`` wall-budget deadline for this stage,
            forwarded to every ``llm.complete`` so each call gets a per-request
            timeout — the only thing that can break a hung upstream from inside the
            executor thread.

    Returns:
        An ``AgentResult`` whose ``raw`` is the agent's final text answer.
    """
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tools_by_name = {t.name: t for t in tools}
    tool_schemas = [t.schema for t in tools] or None

    for _ in range(max(1, max_iter)):
        resp = llm.complete(messages, tools=tool_schemas, deadline=deadline)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            content = (getattr(msg, "content", None) or "").strip()
            if task_cb:
                _safe(task_cb, "task complete")
            return AgentResult(raw=content)

        # Record the assistant turn (with its tool-call requests) before answering them.
        messages.append(_message_to_dict(msg))
        for tc in tool_calls:
            name = tc.function.name
            args = _parse_args(tc.function.arguments)
            # Stuck-loop guard first — a CapExceeded here must abort the run, so it
            # is intentionally NOT wrapped in the tool try/except below.
            if tracker is not None:
                tracker.check(name, args)
            if step_cb:
                _safe(step_cb, f"tool: {name}")
            spec = tools_by_name.get(name)
            if spec is None:
                result = f"(unknown tool {name!r})"
            else:
                try:
                    result = spec.fn(**args)
                except Exception as exc:  # tolerate tool errors; let the model recover
                    logger.warning("tool %s failed: %s", name, exc)
                    result = f"(tool {name!r} failed: {exc})"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": str(result),
            })

    # Iteration budget exhausted — force a final, tool-less answer.
    resp = llm.complete(messages, tools=None, deadline=deadline)
    content = (getattr(resp.choices[0].message, "content", None) or "").strip()
    if task_cb:
        _safe(task_cb, "task complete (max iterations)")
    return AgentResult(raw=content)


def _safe(cb: Callable[[str], None], label: str) -> None:
    """Call a progress callback, swallowing any error (logging must never break a run)."""
    try:
        cb(label)
    except Exception:
        pass
