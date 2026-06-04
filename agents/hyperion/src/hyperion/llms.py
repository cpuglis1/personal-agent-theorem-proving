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
        """Construct a CrewAI ``LLM`` augmented with Hyperion bookkeeping.

        Args:
            *args: Positional args forwarded verbatim to ``crewai.LLM`` (e.g. ``model``).
            hyperion_task_id: The Hyperion task id. When set, per-agent usage caps
                are enforced on every ``call`` (see ``call``). Also used upstream as
                the Langfuse session_id so all turns of one task group together.
            hyperion_role: The agent role (planner/worker/critic/...) this LLM serves.
                Combined with ``hyperion_task_id`` to look up the role's spend cap.
            hyperion_fallback_model: Optional concrete ``openai/<model>`` string used
                for a single retry if the primary completion raises (see ``call``).
            **kwargs: Keyword args forwarded verbatim to ``crewai.LLM`` (e.g.
                ``base_url``, ``api_key``, ``temperature``, ``extra_body``).

        Side effects:
            Stashes the three Hyperion fields on the instance; no I/O.
        """
        super().__init__(*args, **kwargs)
        self._hyperion_task_id = hyperion_task_id
        self._hyperion_role = hyperion_role
        # Concrete `openai/<model>` to retry once if the primary call raises. This is
        # a per-agent fallback layered *above* LiteLLM's own alias-group fallbacks —
        # it covers the case where the record pins a concrete model that has no proxy
        # fallback configured.
        self._hyperion_fallback_model = hyperion_fallback_model

    def set_callbacks(self, callbacks: Any) -> None:
        """Install litellm callbacks, always preserving Hyperion's usage logger.

        CrewAI calls this both at construction and before every agent turn, each
        time *replacing* ``litellm.callbacks`` wholesale. Left alone, that evicts
        Hyperion's usage logger right before a completion. This override re-appends
        the singleton usage logger so both CrewAI's own handler and ours fire.

        Args:
            callbacks: The callback list CrewAI wants to install (may be ``None``).

        Side effects:
            Sets the process-global ``litellm.callbacks`` (via ``super()``) to the
            given list with the usage logger appended.
        """
        from hyperion.usage import get_logger

        logger = get_logger()
        # Append (don't overwrite) so CrewAI's TokenCalcHandler and our logger coexist.
        cbs = list(callbacks or [])
        if logger not in cbs:
            cbs = cbs + [logger]
        super().set_callbacks(cbs)

    def call(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a completion, enforcing spend caps and a single fallback retry.

        Cap enforcement lives here (one layer above litellm) because litellm's
        ``Logging.pre_call`` swallows raises from its own pre-call callbacks; gating
        at this level lets a ``CapExceeded`` propagate through CrewAI's executor up
        to the runner's handler.

        Args:
            messages: The chat messages payload passed through to ``crewai.LLM.call``.
            *args: Additional positional args forwarded to ``super().call``.
            **kwargs: Additional keyword args forwarded to ``super().call`` (CrewAI
                may inject ``callbacks=...`` here, which routes through
                ``set_callbacks``).

        Returns:
            The completion result from ``crewai.LLM.call`` (typically a str).

        Raises:
            CapExceeded: If this task/role has hit its configured spend cap. Never
                swallowed into a fallback retry — caps are a deliberate abort.
            Exception: Any error from the primary model when no fallback is
                configured, or the fallback's error if the retry also fails.

        Side effects:
            May record usage and emit a warning log line when falling back.
        """
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
    node_id: str | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    fallback_model: str | None = None,
    extra_tags: list[str] | None = None,
) -> LLM:
    """Construct a configured ``HyperionLLM`` pointed at the LiteLLM proxy.

    Central factory shared by every public builder below. It assembles the
    OpenAI-compatible ``openai/<model>`` target, attaches Langfuse/usage metadata
    when a ``task_id`` is present, and wires through the optional fallback model.

    Args:
        model: The LiteLLM model alias or concrete name (without the ``openai/``
            prefix, which is added here).
        temperature: Sampling temperature passed to the model.
        task_id: Hyperion task id. When set, enables cap enforcement and adds the
            Langfuse session/trace metadata block.
        agent_role: Role label used in tags and the Langfuse generation name; also
            keys the per-agent cap lookup.
        node_id: Optional workflow-node id this call ran under. Recorded on the
            trace event so the trace UI can attribute calls to the exact node (an
            agent may run in more than one node). Distinct from ``agent_role``,
            which stays the agent id for cap/Langfuse purposes.
        top_p: Optional nucleus-sampling value; only sent when not ``None``.
        max_tokens: Optional output token cap; only sent when not ``None``.
        fallback_model: Optional alias/name retried once on primary failure;
            prefixed with ``openai/`` to match the primary target form.
        extra_tags: Optional extra Langfuse tags (e.g. ``["meta-prompt"]``) merged
            into the default ``hyperion``/role tags.

    Returns:
        A ready-to-use ``HyperionLLM`` instance.
    """
    # Holds optional kwargs assembled conditionally, then splatted into the ctor.
    extra: dict = {}
    if task_id:
        # Langfuse picks up these fields from LiteLLM's metadata block; the usage
        # logger reads `tags` to classify each call (e.g. "meta-prompt").
        tags = ["hyperion", agent_role or "crew", *(extra_tags or [])]
        metadata = {
            "session_id": task_id,
            "trace_user_id": "hyperion",
            "tags": tags,
            "generation_name": f"hyperion/{agent_role or 'agent'}",
        }
        if node_id:
            metadata["node_id"] = node_id
        extra["extra_body"] = {"metadata": metadata}
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
    node_id: str | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    fallback_alias: str | None = None,
) -> LLM:
    """Build an LLM from an agent record's fields (data-driven path, Phase 1).

    With model_alias='smart'/'worker' and the seeded temperatures, this is
    identical to the planner_llm/worker_llm factories the old code used.

    ``fallback_alias`` (the record's fallback model) is retried once if the
    primary call raises — see HyperionLLM.call.

    ``node_id`` (the workflow node this call runs under) is recorded on the trace
    event so the trace UI can attribute calls to the exact node.
    """
    return _make_llm(
        model_alias,
        temperature=temperature,
        task_id=task_id,
        agent_role=agent_role,
        node_id=node_id,
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
