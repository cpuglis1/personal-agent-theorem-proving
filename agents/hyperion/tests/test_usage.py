"""Test suite for per-agent token accounting and cap enforcement (Phase 8).

This module exercises ``hyperion.usage``, the subsystem that tracks how many input
and output tokens each agent role consumes during a Hyperion task run and that
aborts a run when a per-agent token budget ("cap") is exceeded.

What the suite covers:
  - Recording raw token deltas and rolling them up into per-agent and per-task totals
    (``record``, ``agent_totals``, ``task_totals``).
  - Cap enforcement at the accounting layer (``check_agent_cap``), including the
    no-cap no-op path.
  - The litellm callback integration: ``HyperionUsageLogger`` reads the ``session_id``
    (task id) and ``role`` out of litellm's request metadata/tags, enforces caps in the
    pre-call hook, and accumulates token usage from the success event's response payload.
  - The logger singleton accessor (``get_logger``).
  - The ``HyperionLLM`` wrapper that re-enforces caps *before* delegating to the network,
    and that keeps the usage logger registered in ``litellm.callbacks``.

Key non-obvious context:
  - Caps are sourced via ``usage._agent_caps(role) -> (input_cap, output_cap)``. Tests
    monkeypatch this function to inject deterministic caps without touching agent config.
  - Cap breaches raise ``CapExceeded`` (defined in ``hyperion.crews.runner``).
  - litellm SWALLOWS exceptions raised inside the pre-call callback, so the real
    enforcement point is ``HyperionLLM.call`` (see ``test_hyperion_llm_gates_on_cap_*``).
  - CrewAI overwrites ``litellm.callbacks`` with its own handler each turn, so
    ``HyperionLLM.set_callbacks`` must re-append the usage logger
    (see ``test_hyperion_llm_set_callbacks_reappends_logger``).
  - Task ids/roles used by the success-event metadata path live under the ``metadata``
    key, while the pre-call hook reads from ``litellm_params.metadata`` — both shapes
    are exercised on purpose.
"""

from __future__ import annotations

import pytest

from hyperion import usage
from hyperion.crews.runner import CapExceeded


@pytest.fixture(autouse=True)
def _clean():
    """Reset the usage accumulator for task ``"t1"`` around every test.

    Applied automatically (``autouse=True``) to isolate the module-level usage
    counters between tests: the task is cleared before the test body runs and
    cleared again afterward so leftover token records can never leak into the
    next test.

    Yields:
        None: control returns to the test between the two ``reset_task`` calls.

    Side effects:
        Mutates global per-task state in ``hyperion.usage`` for task id ``"t1"``.
    """
    usage.reset_task("t1")
    yield
    usage.reset_task("t1")


def test_record_and_totals():
    """Recorded token deltas roll up correctly per-agent and per-task.

    Records several (input, output) deltas across two roles and asserts that
    ``agent_totals`` sums a single role and ``task_totals`` reports the task-wide
    input/output sums plus a per-agent breakdown under ``by_agent``.
    """
    usage.record("t1", "researcher", 100, 20)
    usage.record("t1", "researcher", 50, 10)
    usage.record("t1", "synthesizer", 5, 5)
    assert usage.agent_totals("t1", "researcher") == (150, 30)
    totals = usage.task_totals("t1")
    assert totals["input_tokens"] == 155
    assert totals["output_tokens"] == 35
    assert totals["by_agent"]["researcher"] == {"input": 150, "output": 30}


def test_check_agent_cap_raises_when_over(monkeypatch):
    """``check_agent_cap`` raises ``CapExceeded`` once recorded input tokens exceed the cap.

    Injects a 120-input-token cap, records 130 input tokens for the researcher,
    and asserts the cap check raises.
    """
    # Pretend the researcher record carries a 120-input-token cap.
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (120, None))
    usage.record("t1", "researcher", 130, 0)
    with pytest.raises(CapExceeded):
        usage.check_agent_cap("t1", "researcher")


def test_check_agent_cap_noop_without_caps(monkeypatch):
    """``check_agent_cap`` is a no-op when no caps are configured for the role.

    With both caps set to ``None``, even very large recorded usage must not raise.
    """
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (None, None))
    usage.record("t1", "researcher", 10_000, 10_000)
    usage.check_agent_cap("t1", "researcher")  # no raise


def test_pre_call_hook_raises_capexceeded(monkeypatch):
    """The litellm pre-call hook enforces the output cap from request metadata.

    Configures a 50-output-token cap, records 80 output tokens, then invokes
    ``log_pre_api_call`` with the task id/role carried under
    ``litellm_params.metadata`` (session_id + tags) and asserts ``CapExceeded``.
    """
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (None, 50))
    usage.record("t1", "researcher", 0, 80)
    logger = usage.HyperionUsageLogger()
    # Pre-call hook resolves task/role from litellm_params.metadata (session_id + tags).
    kwargs = {"litellm_params": {"metadata": {"session_id": "t1", "tags": ["hyperion", "researcher"]}}}
    with pytest.raises(CapExceeded):
        logger.log_pre_api_call("m", [], kwargs)


def test_success_event_accumulates_from_metadata():
    """The litellm success event records token usage from the response payload.

    Feeds ``log_success_event`` a request carrying task id/role under ``metadata``
    and a fake response whose ``usage`` reports prompt/completion tokens, then
    asserts those tokens were recorded against the planner role.
    """
    logger = usage.HyperionUsageLogger()
    # Success event resolves task/role from top-level metadata (session_id + tags).
    kwargs = {"metadata": {"session_id": "t1", "tags": ["hyperion", "planner"]}}

    class Resp:
        """Minimal stand-in for a litellm response exposing only ``usage``."""

        usage = {"prompt_tokens": 42, "completion_tokens": 7}

    logger.log_success_event(kwargs, Resp(), 0, 1)
    assert usage.agent_totals("t1", "planner") == (42, 7)


def test_get_logger_is_singleton():
    """``get_logger`` returns the same logger instance on repeated calls.

    Guards the singleton contract that lets the logger be registered once in
    ``litellm.callbacks`` and reliably looked up later.
    """
    assert usage.get_logger() is usage.get_logger()


def test_hyperion_llm_gates_on_cap_before_delegating(monkeypatch):
    """HyperionLLM.call must raise CapExceeded *before* hitting the network —
    litellm swallows the pre-call callback raise, so this is the real enforcer."""
    from hyperion.crews.runner import CapExceeded
    from hyperion.llms import HyperionLLM

    # __new__ bypasses CrewAI's __init__ so the test never opens a network client.
    llm = HyperionLLM.__new__(HyperionLLM)  # skip CrewAI __init__ (no network)
    # Wire up the attributes HyperionLLM.call relies on to identify the task/role.
    llm._hyperion_task_id = "t1"
    llm._hyperion_role = "researcher"

    # Cap input at 5 tokens, then record 10 already-used → over budget.
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (5, None))
    usage.record("t1", "researcher", 10, 0)

    # Spy on the parent class's call() to prove it is never reached once over-cap.
    delegated = {"called": False}
    monkeypatch.setattr(
        HyperionLLM.__bases__[0], "call",
        lambda self, *a, **k: delegated.__setitem__("called", True),
    )

    with pytest.raises(CapExceeded):
        llm.call([{"role": "user", "content": "hi"}])
    # The cap gate must fire before delegation, so the parent call stays untouched.
    assert delegated["called"] is False


def test_hyperion_llm_set_callbacks_reappends_logger():
    """CrewAI overwrites litellm.callbacks with its own TokenCalcHandler each turn;
    our set_callbacks override must keep the usage logger present alongside it."""
    import litellm

    from hyperion.llms import HyperionLLM

    llm = HyperionLLM.__new__(HyperionLLM)
    llm.set_callbacks(["crewai_token_handler"])
    assert usage.get_logger() in litellm.callbacks
