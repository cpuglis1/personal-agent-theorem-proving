"""Phase 2 — the owned LiteLLM tool-calling loop (CrewAI replacement).

Covers ``hyperion.agent_loop.run_agent_loop`` and the registry/agent wiring that
feeds it:
  - returns the model's content when it stops calling tools;
  - executes a tool call (parsed args → ``fn(**args)``), threads the result back
    into the conversation, and continues;
  - the stuck-loop ``ToolCallTracker`` aborts a wedged loop with ``CapExceeded``;
  - the iteration budget forces a final, tool-less completion;
  - a tool error is returned to the model as data (the loop keeps going);
  - ``registry.build_tools`` yields ``ToolSpec`` descriptors and ``build_agent``
    assembles the persona system prompt + tools.

The LLM is a scripted fake (no network): each ``complete`` returns the next
pre-canned response shaped like a LiteLLM ``ModelResponse``.
"""

from __future__ import annotations

import copy

import pytest

from hyperion.agent_loop import Agent, AgentResult, ToolSpec, run_agent_loop
from hyperion.crews.runner import CapExceeded, ToolCallTracker


# ---------------------------------------------------------------------------
# Scripted LiteLLM stand-ins
# ---------------------------------------------------------------------------


class _Func:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _Func(name, arguments)


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message):
        self.message = message


class _Resp:
    def __init__(self, message):
        self.choices = [_Choice(message)]


class ScriptedLlm:
    """Returns pre-canned responses in order; records each call's messages + tools."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, messages, tools=None, deadline=None):
        # Snapshot the conversation so tests can assert on threaded tool results.
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "deadline": deadline})
        return self.responses.pop(0)


def _answer(text):
    return _Resp(_Message(content=text))


def _calls_tool(call_id, name, arguments):
    return _Resp(_Message(tool_calls=[_ToolCall(call_id, name, arguments)]))


def _echo_tool(recorder=None):
    def fn(text=""):
        if recorder is not None:
            recorder.append(text)
        return f"echoed:{text}"
    return ToolSpec(name="echo", description="echo the input",
                    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                    fn=fn)


# ---------------------------------------------------------------------------
# Loop behavior
# ---------------------------------------------------------------------------


def test_returns_content_when_no_tool_calls():
    llm = ScriptedLlm([_answer("the final answer")])
    result = run_agent_loop(system="s", user="u", tools=[], llm=llm, max_iter=5)
    assert isinstance(result, AgentResult)
    assert result.raw == "the final answer"


def test_executes_tool_then_returns_answer():
    recorder: list[str] = []
    tool = _echo_tool(recorder)
    llm = ScriptedLlm([
        _calls_tool("c1", "echo", '{"text": "hello"}'),
        _answer("done"),
    ])
    result = run_agent_loop(system="s", user="u", tools=[tool], llm=llm, max_iter=5)
    assert result.raw == "done"
    # The tool ran with the parsed arguments.
    assert recorder == ["hello"]
    # The second completion saw the tool result threaded into the conversation.
    second_msgs = llm.calls[1]["messages"]
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "echoed:hello"
    # And the first call was offered the tool schema.
    assert llm.calls[0]["tools"][0]["function"]["name"] == "echo"


def test_tracker_aborts_stuck_loop():
    tool = _echo_tool()
    # cap=2: the second identical call trips the guard.
    tracker = ToolCallTracker(cap=2)
    llm = ScriptedLlm([
        _calls_tool("c1", "echo", '{"text": "same"}'),
        _calls_tool("c2", "echo", '{"text": "same"}'),
        _answer("unreached"),
    ])
    with pytest.raises(CapExceeded):
        run_agent_loop(system="s", user="u", tools=[tool], llm=llm, max_iter=5,
                       tracker=tracker)


def test_max_iter_forces_final_answer():
    tool = _echo_tool()
    llm = ScriptedLlm([
        _calls_tool("c1", "echo", '{"text": "x"}'),  # iteration 1 (budget exhausted)
        _answer("forced final"),                      # the tool-less forced completion
    ])
    result = run_agent_loop(system="s", user="u", tools=[tool], llm=llm, max_iter=1)
    assert result.raw == "forced final"
    # The forced final completion is made without tools.
    assert llm.calls[-1]["tools"] is None


def test_tool_error_is_returned_to_model():
    def boom(**kwargs):
        raise RuntimeError("kaboom")

    tool = ToolSpec(name="echo", description="", parameters={"type": "object", "properties": {}},
                    fn=boom)
    llm = ScriptedLlm([
        _calls_tool("c1", "echo", "{}"),
        _answer("recovered"),
    ])
    result = run_agent_loop(system="s", user="u", tools=[tool], llm=llm, max_iter=5)
    assert result.raw == "recovered"
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "failed" in tool_msgs[0]["content"]


def test_unknown_tool_is_reported_not_fatal():
    llm = ScriptedLlm([
        _calls_tool("c1", "ghost", "{}"),
        _answer("ok"),
    ])
    result = run_agent_loop(system="s", user="u", tools=[], llm=llm, max_iter=5)
    assert result.raw == "ok"
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "unknown tool" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# ToolSpec + registry/agent wiring
# ---------------------------------------------------------------------------


def test_toolspec_schema_shape():
    spec = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}},
                    fn=lambda: "x")
    assert spec.schema == {
        "type": "function",
        "function": {"name": "t", "description": "d",
                     "parameters": {"type": "object", "properties": {}}},
    }


def test_build_tools_returns_toolspecs_with_working_fns(tmp_path):
    from hyperion.agents.registry import build_tools
    from hyperion.config import settings

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(settings, "tasks_dir", tmp_path)
        tools = build_tools(["workspace_write", "workspace_read"], "t1")
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"workspace_write", "workspace_read"}
    assert all(isinstance(t, ToolSpec) for t in tools)
    # The wrapped callables are the real tool logic.
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(settings, "tasks_dir", tmp_path)
        by_name["workspace_write"].fn(path="notes/x.md", content="hi")
        assert "hi" in by_name["workspace_read"].fn(path="notes/x.md")


def test_build_agent_assembles_persona_and_tools():
    from hyperion.crews.runner import build_agent
    from hyperion.agents.registry import load_agent

    record = load_agent("researcher")
    agent = build_agent(record, "t1", node_id="n1")
    assert isinstance(agent, Agent)
    assert record.role in agent.system          # persona role present in system prompt
    assert agent.max_iter == record.max_iter
    assert len(agent.tools) == len(record.tools)
    assert all(isinstance(t, ToolSpec) for t in agent.tools)
