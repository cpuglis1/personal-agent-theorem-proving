"""Per-agent token accounting + cap enforcement (PLAN_UNIFIED.md Phase 8)."""

from __future__ import annotations

import pytest

from hyperion import usage
from hyperion.crews.runner import CapExceeded


@pytest.fixture(autouse=True)
def _clean():
    usage.reset_task("t1")
    yield
    usage.reset_task("t1")


def test_record_and_totals():
    usage.record("t1", "researcher", 100, 20)
    usage.record("t1", "researcher", 50, 10)
    usage.record("t1", "synthesizer", 5, 5)
    assert usage.agent_totals("t1", "researcher") == (150, 30)
    totals = usage.task_totals("t1")
    assert totals["input_tokens"] == 155
    assert totals["output_tokens"] == 35
    assert totals["by_agent"]["researcher"] == {"input": 150, "output": 30}


def test_check_agent_cap_raises_when_over(monkeypatch):
    # Pretend the researcher record carries a 120-input-token cap.
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (120, None))
    usage.record("t1", "researcher", 130, 0)
    with pytest.raises(CapExceeded):
        usage.check_agent_cap("t1", "researcher")


def test_check_agent_cap_noop_without_caps(monkeypatch):
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (None, None))
    usage.record("t1", "researcher", 10_000, 10_000)
    usage.check_agent_cap("t1", "researcher")  # no raise


def test_pre_call_hook_raises_capexceeded(monkeypatch):
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (None, 50))
    usage.record("t1", "researcher", 0, 80)
    logger = usage.HyperionUsageLogger()
    kwargs = {"litellm_params": {"metadata": {"session_id": "t1", "tags": ["hyperion", "researcher"]}}}
    with pytest.raises(CapExceeded):
        logger.log_pre_api_call("m", [], kwargs)


def test_success_event_accumulates_from_metadata():
    logger = usage.HyperionUsageLogger()
    kwargs = {"metadata": {"session_id": "t1", "tags": ["hyperion", "planner"]}}

    class Resp:
        usage = {"prompt_tokens": 42, "completion_tokens": 7}

    logger.log_success_event(kwargs, Resp(), 0, 1)
    assert usage.agent_totals("t1", "planner") == (42, 7)


def test_get_logger_is_singleton():
    assert usage.get_logger() is usage.get_logger()


def test_hyperion_llm_gates_on_cap_before_delegating(monkeypatch):
    """HyperionLLM.call must raise CapExceeded *before* hitting the network —
    litellm swallows the pre-call callback raise, so this is the real enforcer."""
    from hyperion.crews.runner import CapExceeded
    from hyperion.llms import HyperionLLM

    llm = HyperionLLM.__new__(HyperionLLM)  # skip CrewAI __init__ (no network)
    llm._hyperion_task_id = "t1"
    llm._hyperion_role = "researcher"

    monkeypatch.setattr(usage, "_agent_caps", lambda role: (5, None))
    usage.record("t1", "researcher", 10, 0)

    delegated = {"called": False}
    monkeypatch.setattr(
        HyperionLLM.__bases__[0], "call",
        lambda self, *a, **k: delegated.__setitem__("called", True),
    )

    with pytest.raises(CapExceeded):
        llm.call([{"role": "user", "content": "hi"}])
    assert delegated["called"] is False


def test_hyperion_llm_set_callbacks_reappends_logger():
    """CrewAI overwrites litellm.callbacks with its own TokenCalcHandler each turn;
    our set_callbacks override must keep the usage logger present alongside it."""
    import litellm

    from hyperion.llms import HyperionLLM

    llm = HyperionLLM.__new__(HyperionLLM)
    llm.set_callbacks(["crewai_token_handler"])
    assert usage.get_logger() in litellm.callbacks
