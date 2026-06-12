"""Regression guard for the CrewAI 0.86.0 version pin.

Hyperion pins ``crewai==0.86.0`` exactly (see ``pyproject.toml`` and
``secondbrain/Projects/AgentArchitecture/CLAUDE.md``) because two of CrewAI's
behaviours are version-sensitive and ``HyperionLLM`` (``src/hyperion/llms.py``)
works *around* them rather than with a public API:

  - **P3 — ``LLM.set_callbacks`` overwrite.** CrewAI replaces ``litellm.callbacks``
    wholesale at construction and before every agent turn, which would evict
    Hyperion's usage logger. ``HyperionLLM.set_callbacks`` re-appends the logger.
  - **P4 — cap enforcement in ``LLM.call``.** litellm swallows raises from its
    pre-call hook, so ``HyperionLLM.call`` gates caps one layer above litellm and
    also implements a single fallback-model retry.

If a CrewAI bump renames or removes ``set_callbacks``/``call``, those overrides
become silent dead code — CrewAI would simply never invoke them, usage logging
and cap enforcement would quietly stop working, and *no other test would fail*
because the existing ``test_usage.py`` cases call our overrides directly. This
suite asserts the assumptions the pin protects so an accidental/automated bump
trips here with a pointer to the workarounds, plus it covers the otherwise
untested fallback-retry branch of ``HyperionLLM.call``.

What this suite covers:
  - The installed CrewAI version still matches the pin.
  - ``crewai.LLM`` still exposes the ``set_callbacks`` and ``call`` methods our
    subclass overrides, and ``HyperionLLM`` genuinely overrides each (its own
    function object, not the inherited one).
  - ``HyperionLLM.call`` retries once on the configured fallback model when the
    primary raises, and restores ``self.model`` afterward.
  - A ``CapExceeded`` is never converted into a fallback retry.
"""

from __future__ import annotations

import pytest


def test_crewai_version_matches_pin():
    """The installed CrewAI must be exactly the pinned 0.86.0.

    A mismatch means the P3/P4 callback workarounds in ``HyperionLLM`` were not
    validated against this version — re-test them before moving the pin.
    """
    import crewai

    assert crewai.__version__ == "0.86.0", (
        f"crewai is {crewai.__version__}, expected pinned 0.86.0. The HyperionLLM "
        "set_callbacks/call workarounds are version-sensitive — re-validate them "
        "(see hyperion/llms.py and AgentArchitecture/CLAUDE.md) before bumping."
    )


def test_crewai_llm_still_exposes_overridden_methods():
    """``crewai.LLM`` must keep the methods ``HyperionLLM`` overrides.

    If CrewAI renames/removes ``set_callbacks`` or ``call``, our overrides stop
    being invoked by CrewAI (silent dead code) rather than erroring — this guard
    is the only thing that catches that.
    """
    from crewai import LLM

    from hyperion.llms import HyperionLLM

    for method in ("set_callbacks", "call"):
        assert hasattr(LLM, method), f"crewai.LLM no longer has .{method}()"
        # Our subclass must define its own function object, not inherit CrewAI's,
        # or the workaround isn't actually in force.
        assert getattr(HyperionLLM, method) is not getattr(LLM, method), (
            f"HyperionLLM no longer overrides .{method}() — workaround lost"
        )


def test_hyperion_llm_retries_once_on_fallback(monkeypatch):
    """When the primary model raises, call() retries once on the fallback model
    and restores self.model afterward."""
    from hyperion.llms import HyperionLLM

    # __new__ skips CrewAI __init__ so no network client is constructed.
    llm = HyperionLLM.__new__(HyperionLLM)
    llm._hyperion_task_id = None        # no cap check on this path
    llm._hyperion_role = None
    llm._hyperion_fallback_model = "openai/fallback-model"
    llm.model = "openai/primary-model"

    # The parent call() fails on the primary model, succeeds on the fallback.
    seen_models: list[str] = []

    def fake_call(self, *a, **k):
        seen_models.append(self.model)
        if self.model == "openai/primary-model":
            raise RuntimeError("primary boom")
        return "fallback-result"

    monkeypatch.setattr(HyperionLLM.__bases__[0], "call", fake_call)

    result = llm.call([{"role": "user", "content": "hi"}])

    assert result == "fallback-result"
    assert seen_models == ["openai/primary-model", "openai/fallback-model"]
    # self.model must be restored to the primary after the fallback attempt.
    assert llm.model == "openai/primary-model"


def test_hyperion_llm_never_retries_on_cap_exceeded(monkeypatch):
    """A CapExceeded is a deliberate abort and must not trigger the fallback retry,
    even when a fallback model is configured."""
    from hyperion import usage
    from hyperion.crews.runner import CapExceeded
    from hyperion.llms import HyperionLLM

    llm = HyperionLLM.__new__(HyperionLLM)
    llm._hyperion_task_id = "t1"
    llm._hyperion_role = "researcher"
    llm._hyperion_fallback_model = "openai/fallback-model"
    llm.model = "openai/primary-model"

    # Force the cap gate (which runs before delegation) to raise CapExceeded.
    monkeypatch.setattr(usage, "_agent_caps", lambda role: (5, None))
    usage.record("t1", "researcher", 10, 0)

    # Spy on the parent call to prove the fallback retry is never attempted.
    delegated = {"called": False}
    monkeypatch.setattr(
        HyperionLLM.__bases__[0], "call",
        lambda self, *a, **k: delegated.__setitem__("called", True),
    )

    with pytest.raises(CapExceeded):
        llm.call([{"role": "user", "content": "hi"}])
    assert delegated["called"] is False
