"""
llms.py — per-role LLM handles pointing at the LiteLLM proxy.

Every call routes through the LiteLLM proxy over the OpenAI-compatible interface
(``openai/<model>``), so no provider SDK credentials are needed beyond the
LITELLM_HYPERION_KEY.

``LlmHandle`` is a thin wrapper over ``litellm.completion`` (Phase 2 — it replaced
the former ``HyperionLLM(crewai.LLM)`` subclass once CrewAI was removed). It owns
exactly what Hyperion needs and nothing more:

  * Langfuse attribution — the ``extra_body.metadata`` block (session_id = task_id,
    tags, generation_name, node_id) so every call of one task groups into a single
    Langfuse session and is attributed to the exact workflow node.
  * Per-agent spend caps — checked *before* each completion via
    ``usage.check_agent_cap`` (litellm swallows raises from its own pre-call hook,
    so the gate lives here, one layer above litellm).
  * A single fallback-model retry when the primary completion raises.

Because we now own the LiteLLM callback list directly (the usage logger is
installed once via ``usage.register()``), the old CrewAI ``set_callbacks`` fight is
gone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from hyperion.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LlmHandle:
    """A configured handle over ``litellm.completion`` for one agent/node.

    Attributes:
        model: The OpenAI-compatible target, e.g. ``openai/<model>``.
        base_url: The LiteLLM proxy base URL.
        api_key: The LiteLLM API key.
        temperature: Sampling temperature.
        top_p: Optional nucleus-sampling value (sent only when set).
        max_tokens: Optional output token cap (sent only when set).
        metadata: Optional Langfuse/usage metadata block, sent via ``extra_body``.
        task_id: Hyperion task id; when set with ``agent_role`` enables cap checks.
        agent_role: Role label used for the per-agent spend-cap lookup.
        fallback_model: Optional ``openai/<model>`` retried once on primary failure.
    """

    model: str
    base_url: str
    api_key: str
    temperature: float = 0.1
    top_p: float | None = None
    max_tokens: int | None = None
    metadata: dict | None = None
    task_id: str | None = None
    agent_role: str | None = None
    fallback_model: str | None = None

    def _kwargs(self, messages: Any, tools: list[dict] | None, timeout: float | None) -> dict:
        """Assemble the ``litellm.completion`` kwargs for one call."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "temperature": self.temperature,
        }
        if self.top_p is not None:
            kwargs["top_p"] = self.top_p
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if timeout is not None:
            # The ONLY knob that can interrupt a stalled upstream from inside the
            # executor thread; litellm raises litellm.Timeout once it elapses.
            kwargs["timeout"] = timeout
        if self.metadata:
            # The proxy reads this for Langfuse; the local usage logger digs it out
            # of optional_params.extra_body.metadata for attribution + cap checks.
            kwargs["extra_body"] = {"metadata": self.metadata}
        if tools:
            kwargs["tools"] = tools
        return kwargs

    def _request_timeout(self, deadline: float | None) -> float | None:
        """Resolve the per-request timeout for one completion.

        Bounds every call by ``min(remaining wall budget, cap_per_call_seconds)``:

          * ``cap_per_call_seconds`` is the ceiling that protects callers with no
            wall deadline (e.g. the meta-prompt / follow-up pipelines) from a
            permanently hung upstream. Disabled (no ceiling) when set to 0.
          * ``deadline`` (a ``time.monotonic()`` value threaded down from the
            stage's remaining wall budget) shortens the timeout as the run nears
            its wall cap, so a single call can never outlive the wall budget.

        Returns ``None`` only when neither bound applies. A non-positive remaining
        budget collapses to a tiny positive value so litellm fails fast rather than
        treating ``<=0`` as "no timeout".
        """
        ceiling = settings.cap_per_call_seconds or None
        if deadline is None:
            return ceiling
        import time

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            remaining = 0.001  # already over budget — force an immediate timeout
        return min(ceiling, remaining) if ceiling else remaining

    def complete(
        self, messages: Any, tools: list[dict] | None = None, deadline: float | None = None
    ) -> Any:
        """Run one completion, enforcing the spend cap and a single fallback retry.

        Args:
            messages: The chat messages payload.
            tools: Optional list of OpenAI/LiteLLM function-tool schemas.
            deadline: Optional ``time.monotonic()`` wall-budget deadline for the
                enclosing stage. Threaded down so each call's per-request timeout is
                ``min(remaining budget, cap_per_call_seconds)`` — the only thing that
                can break a hung upstream from inside the executor thread.

        Returns:
            The raw LiteLLM ``ModelResponse`` (``.choices[0].message`` carries the
            content and any ``tool_calls``).

        Raises:
            CapExceeded: If this task/role has hit its configured spend cap — a
                deliberate abort, never swallowed into the fallback retry.
            Exception: The primary model's error when no fallback is configured, or
                the fallback's error if the retry also fails. A ``litellm.Timeout``
                flows through the normal single fallback retry — but the fallback's
                timeout is recomputed against the same ``deadline``, so it stays
                bounded and cannot reintroduce the hang; if the wall budget is spent
                it fails fast and the timeout propagates to fail the run.
        """
        if self.task_id and self.agent_role:
            from hyperion.usage import check_agent_cap

            check_agent_cap(self.task_id, self.agent_role)

        import litellm

        try:
            return litellm.completion(**self._kwargs(messages, tools, self._request_timeout(deadline)))
        except Exception as exc:
            from hyperion.crews.runner import CapExceeded

            # Caps are a deliberate abort — never swallow them into a fallback retry.
            if isinstance(exc, CapExceeded) or not self.fallback_model:
                raise
            logger.warning(
                "Primary model %s failed (%s); retrying once on fallback %s",
                self.model, exc, self.fallback_model,
            )
            # Recompute the timeout against the same deadline so the fallback is
            # bounded by whatever wall budget remains (never a fresh full ceiling).
            kwargs = self._kwargs(messages, tools, self._request_timeout(deadline))
            kwargs["model"] = self.fallback_model
            return litellm.completion(**kwargs)

    def complete_text(self, messages: Any) -> str:
        """Convenience: run a tool-less completion and return the content string.

        Used by lightweight callers (e.g. the meta-prompt pipeline) that just want
        a single text answer rather than the full tool-calling loop.
        """
        resp = self.complete(messages, tools=None)
        return (getattr(resp.choices[0].message, "content", None) or "").strip()


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
) -> LlmHandle:
    """Construct a configured ``LlmHandle`` pointed at the LiteLLM proxy.

    Central factory shared by every public builder. It assembles the
    OpenAI-compatible ``openai/<model>`` target, attaches Langfuse/usage metadata
    when a ``task_id`` is present, and wires through the optional fallback model.

    Args:
        model: The LiteLLM model alias or concrete name (without the ``openai/``
            prefix, which is added here).
        temperature: Sampling temperature.
        task_id: Hyperion task id. When set, enables cap enforcement and adds the
            Langfuse session/trace metadata block.
        agent_role: Role label used in tags and the Langfuse generation name; also
            keys the per-agent cap lookup.
        node_id: Optional workflow-node id this call runs under, recorded on the
            trace event so the trace UI can attribute calls to the exact node.
        top_p: Optional nucleus-sampling value; only sent when not ``None``.
        max_tokens: Optional output token cap; only sent when not ``None``.
        fallback_model: Optional alias/name retried once on primary failure;
            prefixed with ``openai/`` to match the primary target form.
        extra_tags: Optional extra Langfuse tags (e.g. ``["meta-prompt"]``) merged
            into the default ``hyperion``/role tags.

    Returns:
        A ready-to-use ``LlmHandle`` instance.
    """
    # Ensure the usage logger is on litellm's global callback list. CrewAI used to
    # (inadvertently) keep it attached per-LLM; now that we call litellm.completion
    # ourselves, the once-only global install is what makes token accounting + trace
    # events fire. Idempotent — a no-op after the first call.
    from hyperion.usage import register as _register_usage

    _register_usage()

    metadata: dict | None = None
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
    return LlmHandle(
        model=f"openai/{model}",
        base_url=settings.litellm_base_url,
        api_key=settings.llm_api_key,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        metadata=metadata,
        task_id=task_id,
        agent_role=agent_role,
        fallback_model=f"openai/{fallback_model}" if fallback_model else None,
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
) -> LlmHandle:
    """Build an ``LlmHandle`` from an agent record's fields (data-driven path).

    ``fallback_alias`` (the record's fallback model) is retried once if the primary
    call raises — see ``LlmHandle.complete``. ``node_id`` (the workflow node this
    call runs under) is recorded on the trace event so the trace UI can attribute
    calls to the exact node.
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
