"""
Workflow compiler — turn a natural-language orchestration instruction into a
runnable workflow DAG (requirement 4.1, "dynamic via prompting").

A user can describe how agents should collaborate in plain language, e.g.
"research it with the Researcher, have the Critic review, then synthesize a
report". ``compile_workflow`` asks an LLM to translate that into a structured
``WorkflowRecord`` — a DAG over the *existing* agent personas — which the caller
persists and runs exactly like a hand-authored workflow. Dynamic prompting (4.1)
therefore produces the same artifact as a pre-defined DAG (4.2): traceable,
resumable, and editable.

Role in the system
------------------
Called from the API's task-submission path (``/tasks`` with ``workflow_prompt``).
The compiled record is validated against the real agent registry (only known
agents, slug ids, acyclic) before it is ever run, so a bad generation fails fast
with a 422 rather than producing a broken run.

Design notes
-----------
- **One LLM call, deterministic prompt.** The model only chooses *structure*
  (which agents, in what order/branches); it never invents agents — the system
  prompt lists the allowed agent ids and the JSON schema, and the output is
  validated with ``validate_workflow``.
- **One repair retry.** If the first generation fails validation, the error is
  fed back once for a fix. Still-invalid output raises ``WorkflowCompileError``.
- **All LLM access via the LiteLLM proxy** (workspace convention) — never a
  provider SDK directly.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from pydantic import ValidationError

from hyperion.agents.registry import AgentRecord
from hyperion.config import settings
from hyperion.crews.workflows import (
    NodeWhen,
    WorkflowNode,
    WorkflowRecord,
    validate_workflow,
)

logger = logging.getLogger(__name__)


class WorkflowCompileError(ValueError):
    """Raised when a natural-language prompt cannot be compiled into a valid
    workflow (the model returned unparseable JSON, or the result still failed
    structural validation after one repair attempt)."""


# Marker prefix for compiled (vs. hand-authored) workflow records, so the UI and
# any future cleanup can tell them apart.
ADHOC_PREFIX = "adhoc-"


def _agent_catalog(agents: list[AgentRecord]) -> str:
    """Render the available agents as a compact bullet list for the prompt."""
    lines = []
    for a in agents:
        if not a.active:
            continue
        desc = a.description or a.role or ""
        lines.append(f"- {a.id}: {a.name} — {desc}")
    return "\n".join(lines) or "(no agents available)"


_SYSTEM_PROMPT = """\
You are a workflow compiler for a multi-agent orchestrator. Translate the user's \
plain-language instruction into a workflow: a directed acyclic graph (DAG) of \
nodes, where each node runs one agent.

Available agents (use ONLY these ids):
{catalog}

Output STRICT JSON (no prose, no code fences) of the form:
{{
  "name": "<=6-word title for this workflow",
  "nodes": [
    {{
      "id": "<slug unique within this workflow>",
      "agent": "<one of the agent ids above>",
      "kind": "plan" | "work" | "synthesize",
      "upstream": ["<ids of nodes that must finish first>"],
      "instruction": "<optional: a specific task for this node, or null>"
    }}
  ]
}}

Rules:
- Use only the agent ids listed above. Never invent an agent.
- Node ids are lowercase slugs (letters, digits, hyphen, underscore), unique within the workflow.
- "upstream" lists node ids (NOT agent ids) that must complete before this node runs. Use it to express order and fan-in/fan-out. Leave it [] for the first node(s).
- The graph must be acyclic.
- "kind" is the node's role: "plan" decomposes the request, "work" researches/executes, "synthesize" writes the final report. Pick the closest fit for each agent's job.
- Prefer a small, clear graph that follows the user's described order. If the user wants a final written result, end with a synthesize node.
- Set "instruction" only when the node needs a task beyond the agent's normal behavior; otherwise use null.
"""


def _call_llm(system: str, user: str) -> str:
    """One deterministic completion via the LiteLLM proxy; returns the raw text."""
    from openai import OpenAI

    client = OpenAI(base_url=settings.litellm_base_url, api_key=settings.llm_api_key)
    resp = client.chat.completions.create(
        model=settings.model_planner,
        temperature=0.0,
        max_tokens=1200,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of a model response, tolerating code fences and
    surrounding prose. Raises ``WorkflowCompileError`` if no object parses."""
    # Strip ```json ... ``` fences if present.
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    # Fall back to the first {...last } span.
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise WorkflowCompileError(f"Model did not return valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise WorkflowCompileError("Model JSON was not an object.")
    return obj


def _build_record(obj: dict, workflow_id: str, prompt: str) -> WorkflowRecord:
    """Construct a ``WorkflowRecord`` from the parsed model object.

    Builds nodes defensively (coercing/justifying optional fields) so a slightly
    loose generation still yields a well-typed record for ``validate_workflow`` to
    judge. Raises ``WorkflowCompileError`` on a structurally unusable shape.
    """
    raw_nodes = obj.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise WorkflowCompileError("Compiled workflow has no nodes.")
    nodes: list[WorkflowNode] = []
    for n in raw_nodes:
        if not isinstance(n, dict):
            raise WorkflowCompileError("A node was not an object.")
        when_obj = n.get("when")
        when = None
        if isinstance(when_obj, dict) and when_obj.get("task_types"):
            when = NodeWhen(task_types=list(when_obj["task_types"]))
        # Clamp kind to a valid value rather than letting an odd label raise.
        kind = n.get("kind", "work")
        if kind not in ("plan", "work", "synthesize"):
            kind = "work"
        try:
            nodes.append(
                WorkflowNode(
                    id=str(n.get("id", "")),
                    agent=str(n.get("agent", "")),
                    kind=kind,
                    upstream=[str(u) for u in (n.get("upstream") or [])],
                    gate_before=bool(n.get("gate_before", False)),
                    instruction=n.get("instruction") or None,
                    when=when,
                )
            )
        except ValidationError as exc:
            raise WorkflowCompileError(f"Invalid node shape: {exc}") from exc
    name = str(obj.get("name") or "").strip() or _fallback_name(prompt)
    return WorkflowRecord(id=workflow_id, name=name, description=prompt.strip(), nodes=nodes)


def _fallback_name(prompt: str) -> str:
    """A short display name derived from the prompt when the model omits one."""
    words = prompt.strip().split()
    return (" ".join(words[:6]) or "Ad-hoc workflow")[:60]


def compile_workflow(prompt: str, agents: list[AgentRecord]) -> WorkflowRecord:
    """Compile a natural-language orchestration instruction into a workflow DAG.

    Args:
        prompt: The user's plain-language description of how agents should work
            together (e.g. "research, then have the critic review, then write it up").
        agents: The available agent records; only the active ones are offered to
            the model and only their ids may appear in the result.

    Returns:
        A validated ``WorkflowRecord`` with a generated ``adhoc-<hex>`` id. The
        caller is responsible for persisting it (so HITL resume and the trace UI
        can reload it by id) and running it.

    Raises:
        WorkflowCompileError: If the prompt is empty, no active agents exist, the
            model output cannot be parsed, or the result fails structural
            validation even after one repair attempt.
    """
    if not (prompt and prompt.strip()):
        raise WorkflowCompileError("Empty workflow prompt.")
    active = [a for a in agents if a.active]
    if not active:
        raise WorkflowCompileError("No active agents to build a workflow from.")

    known_ids = {a.id for a in active}
    workflow_id = f"{ADHOC_PREFIX}{uuid.uuid4().hex[:8]}"
    system = _SYSTEM_PROMPT.format(catalog=_agent_catalog(active))

    # First attempt.
    text = _call_llm(system, prompt.strip())
    obj = _extract_json(text)
    record = _build_record(obj, workflow_id, prompt)
    try:
        validate_workflow(record, known_ids)
        return record
    except ValueError as exc:
        # Bind to an outer name — Python clears the `except ... as` variable when
        # the block exits, so we can't reference it in the repair step below.
        first_err = str(exc)
        logger.info("compile_workflow: first attempt invalid (%s); retrying once", first_err)

    # One repair retry: feed the error back and ask for a corrected version.
    repair = (
        f"{prompt.strip()}\n\nYour previous answer was invalid: {first_err}\n"
        f"Previous answer:\n{json.dumps(obj)}\n"
        "Return corrected STRICT JSON only."
    )
    text2 = _call_llm(system, repair)
    obj2 = _extract_json(text2)
    record2 = _build_record(obj2, workflow_id, prompt)
    try:
        validate_workflow(record2, known_ids)
        return record2
    except ValueError as second_err:
        raise WorkflowCompileError(
            f"Could not compile a valid workflow from the prompt: {second_err}"
        ) from second_err
