"""
usage.py — per-task / per-agent token accounting and cap enforcement (Phase 8).

Every Hyperion LLM call already tags its LiteLLM request with a metadata block
(``session_id`` = task_id, ``tags`` = ["hyperion", agent_role]; see ``llms.py``).
A LiteLLM ``CustomLogger`` reads that block to:

  * accumulate input/output tokens per (task_id, agent_role) on each success, and
  * raise ``CapExceeded`` *before* the next call once an agent's record-level
    ``thresholds.max_input_tokens`` / ``max_output_tokens`` is reached.

The runner already catches ``CapExceeded`` and aborts the run, so enforcement is
additive — no runner edit needed. Tracking powers ``GET /metrics`` usage bars.

Token attribution is best-effort: if the metadata block cannot be located in the
LiteLLM callback kwargs (provider/version differences), accounting is simply
skipped for that call rather than guessed.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

# (task_id, agent_role) -> [input_tokens, output_tokens]
_usage: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Accounting API (also used directly by tests)
# ---------------------------------------------------------------------------


def record(task_id: str, agent_role: str, in_tokens: int, out_tokens: int) -> None:
    if not task_id:
        return
    with _lock:
        bucket = _usage[(task_id, agent_role or "unknown")]
        bucket[0] += int(in_tokens or 0)
        bucket[1] += int(out_tokens or 0)


def agent_totals(task_id: str, agent_role: str) -> tuple[int, int]:
    with _lock:
        bucket = _usage.get((task_id, agent_role), [0, 0])
        return bucket[0], bucket[1]


def task_totals(task_id: str) -> dict[str, Any]:
    """Aggregate usage for one task: totals plus a per-agent breakdown."""
    with _lock:
        total_in = total_out = 0
        by_agent: dict[str, dict[str, int]] = {}
        for (tid, role), (i, o) in _usage.items():
            if tid != task_id:
                continue
            total_in += i
            total_out += o
            by_agent[role] = {"input": i, "output": o}
    return {"input_tokens": total_in, "output_tokens": total_out, "by_agent": by_agent}


def all_agent_totals() -> dict[str, dict[str, int]]:
    """Lifetime token totals per agent across all tasks (for /metrics)."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0})
    with _lock:
        for (_tid, role), (i, o) in _usage.items():
            out[role]["input"] += i
            out[role]["output"] += o
    return dict(out)


def reset_task(task_id: str) -> None:
    with _lock:
        for key in [k for k in _usage if k[0] == task_id]:
            del _usage[key]


def _agent_caps(agent_role: str) -> tuple[int | None, int | None]:
    """Look up an agent record's token thresholds. Non-record roles (e.g. the
    'cheap' sub-call alias) have no record → no caps."""
    try:
        from hyperion.agents.registry import load_agent

        th = load_agent(agent_role).thresholds
        return th.max_input_tokens, th.max_output_tokens
    except Exception:
        return None, None


def check_agent_cap(task_id: str, agent_role: str) -> None:
    """Raise CapExceeded if the agent has blown its per-record token threshold."""
    if not task_id or not agent_role:
        return
    cap_in, cap_out = _agent_caps(agent_role)
    if cap_in is None and cap_out is None:
        return
    used_in, used_out = agent_totals(task_id, agent_role)
    if cap_in is not None and used_in >= cap_in:
        _raise(agent_role, "input", used_in, cap_in)
    if cap_out is not None and used_out >= cap_out:
        _raise(agent_role, "output", used_out, cap_out)


def _raise(agent_role: str, kind: str, used: int, cap: int) -> None:
    # Lazy import keeps this module free of a runner import cycle.
    from hyperion.crews.runner import CapExceeded

    raise CapExceeded(
        f"Agent '{agent_role}' exceeded its {kind}-token cap "
        f"({used} >= {cap}). Aborting run."
    )


# ---------------------------------------------------------------------------
# LiteLLM callback metadata extraction
# ---------------------------------------------------------------------------


def _dig_metadata(kwargs: dict) -> dict:
    """Find the metadata block we set via extra_body, wherever LiteLLM stashed it."""
    candidates = [
        kwargs.get("litellm_params", {}).get("metadata"),
        kwargs.get("metadata"),
        kwargs.get("optional_params", {}).get("extra_body", {}).get("metadata"),
        kwargs.get("optional_params", {}).get("metadata"),
    ]
    for c in candidates:
        if isinstance(c, dict) and ("session_id" in c or "tags" in c):
            return c
    return {}


def _attribution(kwargs: dict) -> tuple[str, str]:
    meta = _dig_metadata(kwargs)
    task_id = str(meta.get("session_id") or "")
    role = "unknown"
    tags = meta.get("tags") or []
    # tags is ["hyperion", agent_role]; take the first non-"hyperion" tag.
    for t in tags:
        if t and t != "hyperion":
            role = str(t)
            break
    return task_id, role


def _usage_from_response(response_obj: Any) -> tuple[int, int]:
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    if usage is None:
        return 0, 0
    get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
    return int(get("prompt_tokens", 0) or 0), int(get("completion_tokens", 0) or 0)


class HyperionUsageLogger(CustomLogger):
    """Accumulate token usage and enforce per-agent caps via LiteLLM callbacks."""

    def log_pre_api_call(self, model, messages, kwargs):  # noqa: D401 (sync hook)
        try:
            task_id, role = _attribution(kwargs)
            check_agent_cap(task_id, role)
        except Exception as exc:  # CapExceeded must propagate; others must not.
            from hyperion.crews.runner import CapExceeded

            if isinstance(exc, CapExceeded):
                raise

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            task_id, role = _attribution(kwargs)
            in_tok, out_tok = _usage_from_response(response_obj)
            record(task_id, role, in_tok, out_tok)
        except Exception:
            pass

    async def async_log_pre_api_call(self, model, messages, kwargs):
        self.log_pre_api_call(model, messages, kwargs)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.log_success_event(kwargs, response_obj, start_time, end_time)


_REGISTERED = False
_LOGGER: HyperionUsageLogger | None = None


def get_logger() -> HyperionUsageLogger:
    """Return the process-wide usage logger singleton.

    Pass this into CrewAI's ``LLM(callbacks=[...])`` so token accounting and cap
    enforcement survive — CrewAI's ``LLM.set_callbacks`` does ``litellm.callbacks =
    callbacks`` (a full overwrite) on every LLM construction, which silently wipes
    anything we installed via ``register()``. Handing it our logger means CrewAI
    sets ``litellm.callbacks = [our_logger]`` instead of ``[]``."""
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = HyperionUsageLogger()
    return _LOGGER


def register() -> None:
    """Install the usage logger on LiteLLM's callback list exactly once.

    Covers direct ``litellm.completion`` calls (e.g. from tools). CrewAI agent
    calls are covered separately via ``get_logger()`` passed to each ``LLM`` —
    see that function for why the global install alone is insufficient."""
    global _REGISTERED
    if _REGISTERED:
        return
    import litellm

    logger = get_logger()
    existing = list(getattr(litellm, "callbacks", []) or [])
    if logger not in existing:
        litellm.callbacks = existing + [logger]
    _REGISTERED = True
