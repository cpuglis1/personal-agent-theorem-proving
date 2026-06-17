"""Follow-up conversation tests (distill + retrieve).

Covers the post-run follow-up machinery in :mod:`hyperion.followup` plus the runner's
``node_outputs.json`` persistence — all against a patched ``tasks_dir`` with the LLM
calls mocked, so no network/model is touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hyperion import followup
from hyperion.config import settings
from hyperion.crews import runner


@pytest.fixture
def anyio_backend():
    """Pin anyio's backend to asyncio so async tests run once."""
    return "asyncio"


def _seed_run(tmp_path, task_id="abcd1234"):
    """Write a node_outputs.json + result.md for a finished run under tmp tasks_dir."""
    d = tmp_path / task_id
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    (d / "artifacts" / "result.md").write_text("# Final\nThe synthesized answer.", encoding="utf-8")
    node_outputs = {
        "plan": {"kind": "plan", "agent": "planner", "instruction": "", "output": "Step 1, step 2."},
        "research": {
            "kind": "work",
            "agent": "researcher",
            "instruction": "",
            "output": "Detailed findings about widgets and gizmos." * 20,
        },
    }
    (d / "node_outputs.json").write_text(json.dumps(node_outputs), encoding="utf-8")
    return task_id


# ---------------------------------------------------------------------------
# Runner persistence
# ---------------------------------------------------------------------------


def test_persist_run_outputs_writes_metadata(tmp_path):
    """_persist_run_outputs maps node ids to {kind, agent, instruction, output}."""
    with patch.object(settings, "tasks_dir", tmp_path):
        ordered = [
            SimpleNamespace(id="plan", kind="plan", agent="planner", instruction=None),
            SimpleNamespace(id="work", kind="work", agent="worker", instruction="do the thing"),
        ]
        runner._persist_run_outputs("t1", ordered, {"plan": "PLAN TEXT", "work": "WORK TEXT"})

        data = json.loads((tmp_path / "t1" / "node_outputs.json").read_text())
        assert data["plan"]["kind"] == "plan"
        assert data["plan"]["agent"] == "planner"
        assert data["work"]["instruction"] == "do the thing"
        assert data["work"]["output"] == "WORK TEXT"


def test_persist_run_outputs_merges_existing(tmp_path):
    """A resumed run accumulates outputs rather than overwriting earlier waves."""
    with patch.object(settings, "tasks_dir", tmp_path):
        (tmp_path / "t1").mkdir(parents=True)
        (tmp_path / "t1" / "node_outputs.json").write_text(
            json.dumps({"plan": {"kind": "plan", "agent": None, "instruction": "", "output": "OLD"}})
        )
        ordered = [SimpleNamespace(id="work", kind="work", agent="w", instruction=None)]
        runner._persist_run_outputs("t1", ordered, {"work": "NEW"})

        data = json.loads((tmp_path / "t1" / "node_outputs.json").read_text())
        assert set(data) == {"plan", "work"}  # earlier wave preserved
        assert data["work"]["output"] == "NEW"


# ---------------------------------------------------------------------------
# Distill: node index
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_node_index(tmp_path):
    """build_node_index summarizes every node and records approx token sizes."""
    task_id = _seed_run(tmp_path)
    fake_llm = MagicMock()
    fake_llm.complete_text.return_value = "A one-line summary."
    with patch.object(settings, "tasks_dir", tmp_path):
        with patch("hyperion.llms._make_llm", return_value=fake_llm):
            await followup.build_node_index(task_id)

    index = json.loads((tmp_path / task_id / "node_index.json").read_text())
    by_id = {e["node_id"]: e for e in index}
    assert set(by_id) == {"plan", "research"}
    assert by_id["plan"]["summary"] == "A one-line summary."
    # research output is much larger than plan → bigger token estimate
    assert by_id["research"]["approx_tokens"] > by_id["plan"]["approx_tokens"]


@pytest.mark.anyio
async def test_build_node_index_noop_without_outputs(tmp_path):
    """No node_outputs.json → no index file, no LLM call."""
    with patch.object(settings, "tasks_dir", tmp_path):
        with patch("hyperion.llms._make_llm") as mk:
            await followup.build_node_index("nope")
            mk.assert_not_called()
    assert not (tmp_path / "nope" / "node_index.json").exists()


# ---------------------------------------------------------------------------
# Retrieve: get_node_output tool
# ---------------------------------------------------------------------------


def test_get_node_output_tool(tmp_path):
    """The retrieval tool returns a node's full text, and lists nodes on a miss."""
    task_id = _seed_run(tmp_path)
    with patch.object(settings, "tasks_dir", tmp_path):
        tools = followup._make_tools(task_id)
        get = {t.name: t for t in tools}["get_node_output"]

        assert "findings about widgets" in get.fn("research")
        miss = get.fn("ghost")
        assert "no node named 'ghost'" in miss
        assert "research" in miss  # available nodes listed


# ---------------------------------------------------------------------------
# Grounding + history
# ---------------------------------------------------------------------------


def test_grounding_block_includes_index_not_full_outputs(tmp_path):
    """Grounding carries the result + compact index, never the full node outputs."""
    task_id = _seed_run(tmp_path)
    with patch.object(settings, "tasks_dir", tmp_path):
        followup._node_index_path(task_id).write_text(
            json.dumps(
                [{"node_id": "research", "kind": "work", "summary": "found stuff", "approx_tokens": 99}]
            )
        )
        block = followup._grounding_block(
            "do research", followup.load_result(task_id), followup.load_node_index(task_id)
        )
    assert "do research" in block
    assert "The synthesized answer." in block
    assert "research" in block and "found stuff" in block
    # the large raw node output must NOT be inlined into the grounding
    assert "gizmos" not in block


def test_compact_history_keeps_recent_verbatim():
    """Short histories are rendered verbatim with no summarization call."""
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    block = followup._compact_history(history)
    assert "User: q1" in block and "Assistant: a1" in block


def test_compact_history_summarizes_overflow():
    """Past the verbatim window, older turns are folded into a summary line."""
    history = [{"role": "user", "content": f"m{i}"} for i in range(followup._MAX_VERBATIM_TURNS + 6)]
    with patch.object(followup, "_summarize_older", return_value="older summary"):
        block = followup._compact_history(history)
    assert "older summary" in block
    # only the verbatim window remains as literal turns
    assert block.count("User: m") == followup._MAX_VERBATIM_TURNS


def test_run_followup_chat_grounds_and_returns(tmp_path):
    """run_followup_chat passes the grounding to the loop and returns its answer."""
    task_id = _seed_run(tmp_path)
    with patch.object(settings, "tasks_dir", tmp_path):
        followup._node_index_path(task_id).write_text(
            json.dumps([{"node_id": "research", "kind": "work", "summary": "found stuff", "approx_tokens": 99}])
        )
        captured = {}

        def fake_loop(*, system, user, tools, llm, max_iter):
            captured["system"] = system
            captured["user"] = user
            captured["tool_names"] = [t.name for t in tools]
            return SimpleNamespace(raw="grounded answer")

        with patch("hyperion.agent_loop.run_agent_loop", side_effect=fake_loop):
            with patch("hyperion.llms._make_llm", return_value=MagicMock()):
                reply = followup.run_followup_chat(task_id, "do research", [], "what did research find?")

    assert reply == "grounded answer"
    assert captured["user"] == "what did research find?"
    assert "get_node_output" in captured["tool_names"]
    assert "found stuff" in captured["system"]  # node index is in the grounding
