"""
meta_tasks.py — configurable post-run meta-prompt pipeline.

Role in the system
------------------
After a Hyperion run finishes the synthesizer stage (the final agent that produces
the polished answer), this module runs a small set of lightweight "meta" LLM prompts
that enrich the run with derived metadata: a short title, suggested follow-up
questions, and topic tags. These are conveniences for the UI and downstream indexing,
not part of the core agent reasoning, so they are intentionally kept cheap.

Design decisions / non-obvious context
--------------------------------------
- Each entry in ``META_TASKS`` fires after the synthesizer stage completes.
- All enabled tasks run in parallel via ``asyncio.gather``.
- Tasks operate on the synthesizer's result *text only* (truncated to 4000 chars),
  not the full conversation transcript — this keeps token cost and latency low.
- Each task uses the cheap model (``settings.model_cheap``) at temperature 0.0 for
  deterministic, inexpensive output.
- Failures are isolated: a meta-task that raises is logged as a warning and swallowed
  so it never breaks the parent run or its sibling meta-tasks.

Configuration
-------------
- To add a meta-task: append an entry to META_TASKS.
- To disable one:    set "enabled": False.
- To edit a prompt:  edit the "prompt" string (must contain a ``{result}`` placeholder).

Outputs and tracing
-------------------
- Results are saved to ``tasks/{task_id}/meta/{id}.txt``.
- The LLM calls are tagged with ["hyperion", "meta/<id>", "meta-prompt"] so they appear
  as prompt_type="meta-prompt" in trace_events and the Trace Flow UI.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Registry of post-run meta-prompts. Each dict requires:
#   "id"      — short slug; used for the output filename ({id}.txt) and the meta/<id> trace tag.
#   "enabled" — bool; tasks default to enabled (see run_meta_tasks) but are listed explicitly here.
#   "prompt"  — template string; MUST contain a "{result}" placeholder, which is filled with the
#               (truncated) synthesizer output before the LLM call.
META_TASKS: list[dict[str, Any]] = [
    {
        "id": "title",
        "enabled": True,
        "prompt": (
            "Generate a concise 3-5 word title with a relevant emoji for the following "
            "research output. Output only the title, nothing else.\n\n{result}"
        ),
    },
    {
        "id": "followups",
        "enabled": True,
        "prompt": (
            "Based on the following research output, suggest 3-5 follow-up questions a "
            "user might want to explore next. Output a numbered list only.\n\n{result}"
        ),
    },
    {
        "id": "tags",
        "enabled": True,
        "prompt": (
            "Analyze the following research output and provide 1-3 broad topic tags and "
            "1-3 specific subtopic tags. Format: broad: tag1, tag2 | specific: tag3, tag4"
            "\n\n{result}"
        ),
    },
]


async def _run_one(task_id: str, meta: dict, result_text: str, out_dir: Path) -> None:
    """Execute a single meta-task and persist its output to disk.

    Builds a cheap LLM, formats the task's prompt template with the (truncated)
    synthesizer result, calls the model off the event loop, and writes the stripped
    response to ``out_dir/{meta['id']}.txt``.

    Args:
        task_id: ID of the parent Hyperion run; used for log lines and to tag the
            LLM call for tracing.
        meta: A single META_TASKS entry with at least "id" and "prompt" keys.
        result_text: The synthesizer's output text. Truncated to the first 4000
            characters before being injected into the prompt to cap token cost.
        out_dir: Directory (tasks/{task_id}/meta) where the result file is written;
            created if it does not already exist.

    Returns:
        None. The result is written as a side effect (a .txt file) rather than returned.

    Raises:
        Nothing. Any exception during the LLM call or file write is caught and logged
        as a warning so a failing meta-task cannot break sibling tasks or the parent run.

    Side effects:
        - Creates ``out_dir`` if missing and writes ``{meta['id']}.txt`` into it.
        - Emits an info log on success and a warning log on failure.
        - Issues one LLM call via the LiteLLM-backed cheap model.
    """
    # Imported lazily inside the function to avoid import-time cycles between the
    # server package and hyperion.config / hyperion.llms.
    from hyperion.config import settings
    from hyperion.llms import _make_llm

    llm = _make_llm(
        settings.model_cheap,
        temperature=0.0,
        task_id=task_id,
        agent_role=f"meta/{meta['id']}",
        extra_tags=["meta-prompt"],
    )

    # Truncate to 4000 chars so the meta-prompt stays cheap regardless of how long
    # the synthesizer output was.
    prompt = meta["prompt"].format(result=result_text[:4000])
    try:
        # llm.call is synchronous (CrewAI/litellm); run off the event loop so the
        # gather below actually parallelizes the meta-tasks.
        response = await asyncio.to_thread(llm.call, [{"role": "user", "content": prompt}])
        # llm.call may return a plain string or an object with a .content attribute,
        # depending on the underlying provider/wrapper; normalize to a string either way.
        text = response if isinstance(response, str) else getattr(response, "content", str(response))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{meta['id']}.txt").write_text(text.strip(), encoding="utf-8")
        logger.info("task %s: meta/%s complete", task_id, meta["id"])
    except Exception as exc:
        logger.warning("task %s: meta/%s failed: %s", task_id, meta["id"], exc)


async def run_meta_tasks(task_id: str, result_text: str) -> None:
    """Run all enabled meta-tasks in parallel for a completed run.

    Entry point for the pipeline, invoked after the synthesizer stage produces its
    final result. Filters META_TASKS to the enabled entries and fans them out with
    ``asyncio.gather`` so every meta-prompt runs concurrently.

    Args:
        task_id: ID of the parent Hyperion run; determines the output directory and
            is used to tag the meta LLM calls.
        result_text: The synthesizer's final output text that the meta-prompts analyze.

    Returns:
        None. Each meta-task writes its own file as a side effect.

    Side effects:
        - Writes one ``{id}.txt`` file per enabled meta-task under
          ``settings.tasks_dir / task_id / "meta"``.

    Notes:
        - No-ops early if there are no enabled tasks or if ``result_text`` is blank.
        - Individual task failures are swallowed inside ``_run_one``; this call does
          not raise on a meta-task error.
    """
    from hyperion.config import settings

    # Tasks are enabled by default; an entry must explicitly set "enabled": False to opt out.
    enabled = [m for m in META_TASKS if m.get("enabled", True)]
    # Skip work entirely when there's nothing to run or no substantive result to analyze.
    if not enabled or not result_text.strip():
        return

    out_dir = settings.tasks_dir / task_id / "meta"
    await asyncio.gather(*[_run_one(task_id, m, result_text, out_dir) for m in enabled])
