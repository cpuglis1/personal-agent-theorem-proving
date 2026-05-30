"""
observability.py — Langfuse tracing helper.

Tracing is configured at the LiteLLM proxy level (success_callback: ["langfuse"]
in litellm_config.yaml). Every LLM call from any agent is automatically logged
as a generation in Langfuse, no SDK glue required on the client side.

This module exposes a helper that builds the LiteLLM `metadata` dict so all
LLM calls for a given task share a session_id (= task_id) and tags. The dict
should be passed to the CrewAI LLM via `additional_kwargs` or to OpenAI calls
via `extra_body={"metadata": ...}`.
"""

from __future__ import annotations


def trace_metadata(task_id: str, agent_role: str | None = None) -> dict:
    """
    Build LiteLLM metadata that groups every call for this task under one
    Langfuse session, with optional per-agent tagging.
    """
    meta: dict = {
        "generation_name": f"hyperion/{agent_role or 'crew'}",
        "trace_user_id": "hyperion",
        "session_id": task_id,
        "tags": ["hyperion", agent_role or "crew"],
    }
    return meta
