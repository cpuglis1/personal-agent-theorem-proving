"""
llms.py — per-role LLM factories pointing at the LiteLLM proxy.

All models use the OpenAI-compatible interface via LiteLLM so no provider
SDK credentials are needed beyond the LITELLM_HYPERION_KEY.

Each factory accepts an optional task_id which becomes the Langfuse session_id —
this groups every LLM call from a single Hyperion task into one Langfuse session
so you can see Planner → Researcher → Synthesizer turns side by side.
"""

from __future__ import annotations

from typing import Any

from crewai import LLM

from hyperion.config import settings


class HyperionLLM(LLM):
    """CrewAI LLM that keeps usage accounting alive and enforces per-agent caps.

    Two CrewAI behaviours fight us, both fixed here:

    1. CrewAI's ``LLM.set_callbacks`` does ``litellm.callbacks = callbacks`` (a full
       overwrite) at construction *and* on every agent turn (it passes its own
       ``TokenCalcHandler`` via ``call(callbacks=...)``). That silently evicts our
       usage logger right before the completion. We override ``set_callbacks`` to
       always re-append the logger, so both CrewAI's handler and ours fire.

    2. The litellm ``log_pre_api_call`` callback can *detect* an over-cap condition
       but can't stop the run — litellm's ``Logging.pre_call`` swallows the raise.
       So we gate in ``call`` (one layer above litellm) where a ``CapExceeded``
       propagates through CrewAI's executor to the runner's handler."""

    def __init__(self, *args: Any, hyperion_task_id: str | None = None,
                 hyperion_role: str | None = None,
                 hyperion_fallback_model: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._hyperion_task_id = hyperion_task_id
        self._hyperion_role = hyperion_role
        # Concrete `openai/<model>` to retry once if the primary call raises. This is
        # a per-agent fallback layered *above* LiteLLM's own alias-group fallbacks —
        # it covers the case where the record pins a concrete model that has no proxy
        # fallback configured.
        self._hyperion_fallback_model = hyperion_fallback_model

    def set_callbacks(self, callbacks: Any) -> None:
        from hyperion.usage import get_logger

        logger = get_logger()
        cbs = list(callbacks or [])
        if logger not in cbs:
            cbs = cbs + [logger]
        super().set_callbacks(cbs)

    def call(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        if self._hyperion_task_id and self._hyperion_role:
            from hyperion.usage import check_agent_cap

            check_agent_cap(self._hyperion_task_id, self._hyperion_role)
        try:
            return super().call(messages, *args, **kwargs)
        except Exception as exc:
            from hyperion.crews.runner import CapExceeded

            # Caps are a deliberate abort — never swallow them into a fallback retry.
            if isinstance(exc, CapExceeded) or not self._hyperion_fallback_model:
                raise
            import logging

            logging.getLogger(__name__).warning(
                "Primary model %s failed (%s); retrying once on fallback %s",
                self.model, exc, self._hyperion_fallback_model,
            )
            primary, self.model = self.model, self._hyperion_fallback_model
            try:
                return super().call(messages, *args, **kwargs)
            finally:
                self.model = primary


def _make_llm(
    model: str,
    *,
    temperature: float = 0.1,
    task_id: str | None = None,
    agent_role: str | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    fallback_model: str | None = None,
) -> LLM:
    extra: dict = {}
    if task_id:
        # Langfuse picks up these fields from LiteLLM's metadata block.
        extra["extra_body"] = {
            "metadata": {
                "session_id": task_id,
                "trace_user_id": "hyperion",
                "tags": ["hyperion", agent_role or "crew"],
                "generation_name": f"hyperion/{agent_role or 'agent'}",
            }
        }
    if top_p is not None:
        extra["top_p"] = top_p
    if max_tokens is not None:
        extra["max_tokens"] = max_tokens
    return HyperionLLM(
        model=f"openai/{model}",
        base_url=settings.litellm_base_url,
        api_key=settings.llm_api_key,
        temperature=temperature,
        hyperion_task_id=task_id,
        hyperion_role=agent_role,
        hyperion_fallback_model=f"openai/{fallback_model}" if fallback_model else None,
        **extra,
    )


def make_agent_llm(
    model_alias: str,
    *,
    temperature: float = 0.1,
    task_id: str | None = None,
    agent_role: str | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    fallback_alias: str | None = None,
) -> LLM:
    """Build an LLM from an agent record's fields (data-driven path, Phase 1).

    With model_alias='smart'/'worker' and the seeded temperatures, this is
    identical to the planner_llm/worker_llm factories the old code used.

    ``fallback_alias`` (the record's fallback model) is retried once if the
    primary call raises — see HyperionLLM.call.
    """
    return _make_llm(
        model_alias,
        temperature=temperature,
        task_id=task_id,
        agent_role=agent_role,
        top_p=top_p,
        max_tokens=max_tokens,
        fallback_model=fallback_alias,
    )


def planner_llm(task_id: str | None = None) -> LLM:
    """High-stakes planning. Alias defaults to 'smart' (Opus → Gemini Pro → GPT-4o)."""
    return _make_llm(settings.model_planner, temperature=0.1, task_id=task_id, agent_role="planner")


def worker_llm(task_id: str | None = None, agent_role: str = "worker") -> LLM:
    """Balanced reasoning/cost. Alias defaults to 'worker' (Sonnet → Gemini Pro → GPT-4o)."""
    return _make_llm(settings.model_worker, temperature=0.2, task_id=task_id, agent_role=agent_role)


def cheap_llm(task_id: str | None = None) -> LLM:
    """Cheap sub-calls. Alias defaults to 'cheap' (Haiku → Gemini Flash → GPT-4o-mini)."""
    return _make_llm(settings.model_cheap, temperature=0.0, task_id=task_id, agent_role="cheap")
