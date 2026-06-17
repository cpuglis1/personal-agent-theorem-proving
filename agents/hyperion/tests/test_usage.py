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
  - The ``LlmHandle`` wrapper (``hyperion.llms``) that re-enforces caps *before*
    delegating to ``litellm.completion``, retries once on the fallback model, and is
    accompanied by ``_make_llm`` installing the usage logger in ``litellm.callbacks``.

Key non-obvious context:
  - Caps are sourced via ``usage._agent_caps(role) -> (input_cap, output_cap)``. Tests
    monkeypatch this function to inject deterministic caps without touching agent config.
  - Cap breaches raise ``CapExceeded`` (defined in ``hyperion.crews.runner``).
  - litellm SWALLOWS exceptions raised inside the pre-call callback, so the real
    enforcement point is ``LlmHandle.complete`` (see ``test_llm_handle_gates_on_cap_*``).
  - Since Phase 2 (CrewAI removed) we own the litellm callback list, so the logger is
    installed once globally via ``register`` (called from ``_make_llm``).
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


def _handle(**overrides):
    """Build an ``LlmHandle`` with safe defaults for the cap/fallback tests."""
    from hyperion.llms import LlmHandle

    kwargs = dict(model="openai/primary-model", base_url="http://x/v1", api_key="k")
    kwargs.update(overrides)
    return LlmHandle(**kwargs)


def test_llm_handle_gates_on_cap_before_completion(monkeypatch):
    """LlmHandle.complete must raise CapExceeded *before* calling litellm.completion —
    litellm swallows the pre-call callback raise, so this is the real enforcer."""
    import litellm

    from hyperion.crews.runner import CapExceeded

    llm = _handle(task_id="t1", agent_role="researcher", fallback_model="openai/fb")

    # Cap input at 5 tokens, then record 10 already-used → over budget.
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (5, None))
    usage.record("t1", "researcher", 10, 0)

    # Spy on litellm.completion to prove it is never reached once over-cap.
    called = {"n": 0}
    monkeypatch.setattr(litellm, "completion",
                        lambda **k: called.__setitem__("n", called["n"] + 1))

    with pytest.raises(CapExceeded):
        llm.complete([{"role": "user", "content": "hi"}])
    # The cap gate fires before delegation, so completion (and the fallback) are untouched.
    assert called["n"] == 0


def test_llm_handle_retries_once_on_fallback(monkeypatch):
    """When the primary model raises, complete() retries once on the fallback model."""
    import litellm

    llm = _handle(fallback_model="openai/fallback-model")  # no task_id → no cap check

    seen: list[str] = []

    def fake_completion(**kwargs):
        seen.append(kwargs["model"])
        if kwargs["model"] == "openai/primary-model":
            raise RuntimeError("primary boom")
        return "FALLBACK_RESP"

    monkeypatch.setattr(litellm, "completion", fake_completion)

    result = llm.complete([{"role": "user", "content": "hi"}])
    assert result == "FALLBACK_RESP"
    assert seen == ["openai/primary-model", "openai/fallback-model"]


def test_llm_handle_never_retries_on_cap_exceeded(monkeypatch):
    """A CapExceeded is a deliberate abort and must not trigger the fallback retry."""
    import litellm

    from hyperion.crews.runner import CapExceeded

    llm = _handle(task_id="t1", agent_role="researcher", fallback_model="openai/fb")
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (5, None))
    usage.record("t1", "researcher", 10, 0)

    called = {"n": 0}
    monkeypatch.setattr(litellm, "completion",
                        lambda **k: called.__setitem__("n", called["n"] + 1))

    with pytest.raises(CapExceeded):
        llm.complete([{"role": "user", "content": "hi"}])
    assert called["n"] == 0


def test_llm_handle_passes_per_call_timeout(monkeypatch):
    """Every completion carries a per-request ``timeout`` — without one, a stalled
    upstream hangs the executor thread forever (the wedged-run a31767d9 root cause)."""
    import litellm

    from hyperion.config import settings

    monkeypatch.setattr(settings, "cap_per_call_seconds", 180)
    llm = _handle()  # no deadline → falls back to the cap_per_call_seconds ceiling

    seen: dict = {}
    monkeypatch.setattr(litellm, "completion", lambda **k: seen.update(k) or "OK")

    llm.complete([{"role": "user", "content": "hi"}])
    assert seen["timeout"] == 180


def test_request_timeout_bounded_by_remaining_wall(monkeypatch):
    """When a stage deadline is threaded down, the per-request timeout is
    ``min(remaining wall budget, cap_per_call_seconds)`` so a single call can never
    outlive the run's wall budget."""
    import time

    from hyperion.config import settings

    monkeypatch.setattr(settings, "cap_per_call_seconds", 180)
    llm = _handle()

    # 10s of wall budget left, well under the 180s ceiling → timeout tracks remaining.
    t = llm._request_timeout(time.monotonic() + 10)
    assert 9.0 < t <= 10.0

    # Already over budget → a tiny positive timeout (fail fast), never <= 0 (= no timeout).
    t_over = llm._request_timeout(time.monotonic() - 5)
    assert 0 < t_over < 1


def test_timeout_fails_run_when_no_fallback(monkeypatch):
    """A timed-out call with no fallback must propagate (failing the run) rather than
    being swallowed — the top-level runner converts it into a ``failed`` result."""
    import litellm

    llm = _handle()  # no fallback configured

    def boom(**kwargs):
        raise litellm.Timeout("stalled", model=kwargs["model"], llm_provider="openai")

    monkeypatch.setattr(litellm, "completion", boom)

    with pytest.raises(litellm.Timeout):
        llm.complete([{"role": "user", "content": "hi"}])


def test_make_llm_registers_usage_logger():
    """``_make_llm`` installs the usage logger on litellm's global callback list so
    token accounting + trace events fire on our own litellm.completion calls."""
    import litellm

    from hyperion.llms import _make_llm

    _make_llm("worker", task_id="t1", agent_role="researcher")
    assert usage.get_logger() in litellm.callbacks
