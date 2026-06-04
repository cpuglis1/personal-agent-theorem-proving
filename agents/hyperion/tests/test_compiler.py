"""Unit tests for the natural-language → workflow compiler (requirement 4.1).

These tests exercise ``hyperion.crews.compiler.compile_workflow`` without any
network/LLM access by monkeypatching the module's ``_call_llm`` helper to return
canned model responses. They cover the happy path (valid JSON → validated
``WorkflowRecord``), JSON extraction from fenced/prose-wrapped output, the
one-shot repair retry, and the failure modes that raise ``WorkflowCompileError``.
"""

from __future__ import annotations

import json

import pytest

from hyperion.agents.registry import AgentRecord
from hyperion.crews import compiler
from hyperion.crews.compiler import ADHOC_PREFIX, WorkflowCompileError, compile_workflow


def _agents() -> list[AgentRecord]:
    """A minimal set of persona agents the compiler may reference."""
    return [
        AgentRecord(id="planner", name="Planner", role="r", goal="g", backstory="b"),
        AgentRecord(id="researcher", name="Researcher", role="r", goal="g", backstory="b"),
        AgentRecord(id="synthesizer", name="Synthesizer", role="r", goal="g", backstory="b"),
        AgentRecord(id="off", name="Inactive", role="r", goal="g", backstory="b", active=False),
    ]


def _valid_payload() -> str:
    """A well-formed compiler response: a 3-node linear research pipeline."""
    return json.dumps(
        {
            "name": "Research and write-up",
            "nodes": [
                {"id": "plan", "agent": "planner", "kind": "plan", "upstream": []},
                {"id": "research", "agent": "researcher", "kind": "work", "upstream": ["plan"]},
                {"id": "synth", "agent": "synthesizer", "kind": "synthesize", "upstream": ["research"]},
            ],
        }
    )


def _patch_llm(monkeypatch, responses):
    """Patch ``_call_llm`` to return successive canned responses (or a single one)."""
    if isinstance(responses, str):
        responses = [responses]
    calls = {"n": 0}

    def fake(system, user):  # noqa: ANN001 - test stub
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(compiler, "_call_llm", fake)
    return calls


def test_compiles_valid_workflow(monkeypatch):
    """A valid model response yields a persisted-shape WorkflowRecord with an
    adhoc id, the prompt as description, and the declared nodes in place."""
    _patch_llm(monkeypatch, _valid_payload())
    wf = compile_workflow("research it then write it up", _agents())

    assert wf.id.startswith(ADHOC_PREFIX)
    assert wf.name == "Research and write-up"
    assert wf.description == "research it then write it up"
    assert [n.id for n in wf.nodes] == ["plan", "research", "synth"]
    assert [n.kind for n in wf.nodes] == ["plan", "work", "synthesize"]
    assert wf.nodes[1].upstream == ["plan"]


def test_extracts_json_from_code_fence(monkeypatch):
    """Output wrapped in a ```json fence and prose is still parsed."""
    fenced = f"Sure! Here is the workflow:\n```json\n{_valid_payload()}\n```\nDone."
    _patch_llm(monkeypatch, fenced)
    wf = compile_workflow("do research", _agents())
    assert len(wf.nodes) == 3


def test_unknown_agent_is_rejected(monkeypatch):
    """A node referencing an agent that is not in the registry fails validation on
    both the first attempt and the repair retry, raising WorkflowCompileError."""
    bad = json.dumps(
        {"name": "x", "nodes": [{"id": "n1", "agent": "ghost", "kind": "work", "upstream": []}]}
    )
    calls = _patch_llm(monkeypatch, bad)  # same bad answer both times
    with pytest.raises(WorkflowCompileError):
        compile_workflow("use the ghost agent", _agents())
    assert calls["n"] == 2  # original + one repair attempt


def test_repair_retry_recovers(monkeypatch):
    """An invalid first answer followed by a valid repair answer compiles cleanly."""
    bad = json.dumps(
        {"name": "x", "nodes": [{"id": "n1", "agent": "ghost", "kind": "work", "upstream": []}]}
    )
    calls = _patch_llm(monkeypatch, [bad, _valid_payload()])
    wf = compile_workflow("research then synthesize", _agents())
    assert len(wf.nodes) == 3
    assert calls["n"] == 2


def test_malformed_json_raises(monkeypatch):
    """Unparseable model output raises WorkflowCompileError rather than crashing."""
    _patch_llm(monkeypatch, "not json at all")
    with pytest.raises(WorkflowCompileError):
        compile_workflow("anything", _agents())


def test_empty_prompt_raises(monkeypatch):
    """An empty/whitespace prompt is rejected before any LLM call."""
    calls = _patch_llm(monkeypatch, _valid_payload())
    with pytest.raises(WorkflowCompileError):
        compile_workflow("   ", _agents())
    assert calls["n"] == 0  # never reached the model


def test_no_active_agents_raises(monkeypatch):
    """With no active agents there is nothing to build a workflow from."""
    _patch_llm(monkeypatch, _valid_payload())
    inactive = [AgentRecord(id="off", name="x", role="r", goal="g", backstory="b", active=False)]
    with pytest.raises(WorkflowCompileError):
        compile_workflow("research", inactive)


def test_compiled_workflow_persists_and_reloads(monkeypatch, tmp_path):
    """A compiled workflow can be saved and reloaded by id — the contract HITL
    resume and the trace viewer rely on for ad-hoc (compiled) runs."""
    from hyperion.config import settings
    from hyperion.crews.workflows import load_workflow, resolve_workflow, save_workflow

    _patch_llm(monkeypatch, _valid_payload())
    monkeypatch.setattr(settings, "config_dir", tmp_path)

    wf = compile_workflow("research then synthesize", _agents())
    save_workflow(wf)

    reloaded = load_workflow(wf.id)
    assert reloaded.id == wf.id
    assert [n.id for n in reloaded.nodes] == ["plan", "research", "synth"]
    # resolve_workflow(id) is the exact call the runner/resume path makes.
    assert resolve_workflow(wf.id).id == wf.id
