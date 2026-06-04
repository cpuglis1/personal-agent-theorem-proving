"""
The single planner output contract (PLAN_UNIFIED.md §4.2).

This module is the one canonical place in Hyperion that defines, parses, and
mutates the planner agent's structured output. The planner writes a markdown
file (`plan.md`) for each task; that file begins with a YAML frontmatter block
whose schema is described by :class:`PlanFrontmatter`. Every downstream consumer
reads the *same* parsed frontmatter rather than re-parsing the planner's prose.

plan.md carries one YAML frontmatter schema that serves every consumer:
  - routing signals      task_type, keywords            (Phase 2)
  - HITL alternatives    options[], selected_option     (Phase 3)
  - auto context         context_brief                  (Phase 4)
  - critic trigger       needs_review                   (existing)

Design notes / non-obvious context:
  - The parser is written ONCE here; the planner record's prompt is edited over
    time to emit more of the schema. The parser is therefore deliberately
    tolerant: it must keep working against plans produced by older or
    not-yet-fully-trained prompts.
  - Graceful degradation is the guiding principle. Missing/malformed input never
    raises out of :func:`parse_plan`; it falls back to safe defaults
    (e.g. missing task_type → "mixed", missing options → one implicit option)
    so the router and HITL flow can always make progress.
  - On-disk location of plan.md is derived from ``settings.tasks_dir`` keyed by
    ``task_id`` (see :func:`_plan_path`), so this module owns both the schema
    and the file layout.
"""

from __future__ import annotations

import re
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from hyperion.config import settings

# Matches a leading YAML frontmatter block delimited by `---` fences at the very
# top of the document. Group 1 captures the YAML body between the fences.
# re.DOTALL lets `.` span newlines so multi-line frontmatter is captured; the
# non-greedy `.*?` stops at the first closing `---` line.
_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class Subtask(BaseModel):
    """A single unit of work within a :class:`PlanOption`.

    Attributes:
        id: Stable identifier for the subtask (planner-assigned).
        description: Human-readable description of what the subtask entails.
            Defaults to empty so older/looser planner output still validates.
    """

    id: str
    description: str = ""


class PlanOption(BaseModel):
    """One candidate approach the planner proposes for a task.

    Multiple options enable the Phase 3 HITL flow, where a human (or the system)
    picks one via ``PlanFrontmatter.selected_option``.

    Attributes:
        id: Stable identifier for this option (referenced by ``selected_option``).
        summary: Short description of the approach this option represents.
        subtasks: Ordered list of :class:`Subtask` items that make up the option.
        est_tool_calls: Optional planner estimate of tool calls required, used as
            a rough cost/effort signal. ``None`` when the planner omits it.
    """

    id: str
    summary: str = ""
    subtasks: list[Subtask] = Field(default_factory=list)
    est_tool_calls: Optional[int] = None


class PlanFrontmatter(BaseModel):
    """Parsed representation of plan.md's YAML frontmatter — the planner contract.

    This is the single shared structure read by routing (Phase 2), HITL option
    selection (Phase 3), automatic context injection (Phase 4), and the critic
    trigger. All fields have defaults so a partially-populated or empty plan
    still produces a valid model.

    Attributes:
        task_id: The task identifier, if the planner echoed it into the plan.
        original_request: The user's original request text, if captured.
        task_type: Routing signal; normalized to one of
            ``"research" | "code" | "mixed"`` (defaults to ``"mixed"``).
        keywords: Routing/retrieval keywords extracted by the planner.
        context_brief: Auto-generated context summary injected for downstream
            agents (Phase 4); ``None`` until populated.
        needs_review: Whether the critic agent should review the result.
        subtasks: Flat list of subtask strings (legacy/simple shape, distinct
            from the richer :class:`Subtask` objects nested under options).
        options: Candidate :class:`PlanOption` approaches (Phase 3 HITL).
        selected_option: ``id`` of the chosen option, if one has been selected.
    """

    task_id: Optional[str] = None
    original_request: Optional[str] = None
    task_type: str = "mixed"                       # research | code | mixed
    keywords: list[str] = Field(default_factory=list)
    context_brief: Optional[str] = None
    needs_review: bool = False
    subtasks: list[str] = Field(default_factory=list)
    options: list[PlanOption] = Field(default_factory=list)
    selected_option: Optional[str] = None

    def active_subtasks(self) -> list[Subtask]:
        """Return the subtasks of whichever option is currently in effect.

        Resolution order:
          1. The option whose ``id`` matches ``selected_option``, if any.
          2. Otherwise the first option (the planner's default/preferred plan).
          3. An empty list when no options exist at all.

        Returns:
            The list of :class:`Subtask` objects for the active option, or an
            empty list when there are no options.
        """
        if not self.options:
            return []
        chosen = None
        if self.selected_option:
            # Find the explicitly selected option by id; may be None if the id
            # doesn't match any option (e.g. stale/invalid selection).
            chosen = next((o for o in self.options if o.id == self.selected_option), None)
        # Fall back to the first option when nothing was selected or the
        # selection didn't resolve.
        return (chosen or self.options[0]).subtasks


def _plan_path(task_id: str):
    """Return the on-disk path to a task's plan.md.

    Args:
        task_id: The task identifier used as the per-task directory name.

    Returns:
        A ``Path`` to ``<settings.tasks_dir>/<task_id>/plan.md`` (the file may
        not exist yet).
    """
    return settings.tasks_dir / task_id / "plan.md"


def parse_plan(task_id: str) -> PlanFrontmatter:
    """Parse a task's plan.md frontmatter into a :class:`PlanFrontmatter`.

    This function never raises on bad input — that is intentional. Every failure
    mode (missing file, no frontmatter block, invalid YAML, non-mapping YAML,
    option shapes the planner hasn't learned yet) degrades to safe defaults
    (notably ``task_type='mixed'``) so the router and HITL flow keep working.

    Args:
        task_id: The task whose plan.md should be read.

    Returns:
        A populated :class:`PlanFrontmatter`. On any parse problem, a default or
        partially-populated model is returned rather than raising.
    """
    path = _plan_path(task_id)
    if not path.exists():
        # No plan written yet -> all defaults.
        return PlanFrontmatter()
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # File exists but has no leading frontmatter block.
        return PlanFrontmatter()
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        # Malformed YAML -> defaults rather than propagating the error.
        return PlanFrontmatter()
    if not isinstance(data, dict):
        # Frontmatter parsed to a scalar/list, not a mapping -> defaults.
        return PlanFrontmatter()
    # Normalize task_type to the known set.
    tt = str(data.get("task_type", "mixed")).strip().lower()
    if tt not in ("research", "code", "mixed"):
        tt = "mixed"
    data["task_type"] = tt
    try:
        return PlanFrontmatter.model_validate(data)
    except Exception:
        # Tolerate option shapes the planner hasn't learned yet: salvage the
        # fields we can validate cheaply (coercing types defensively) and drop
        # the parts that failed validation rather than losing the whole plan.
        return PlanFrontmatter(
            task_type=tt,
            keywords=list(data.get("keywords") or []),
            needs_review=bool(data.get("needs_review", False)),
            subtasks=[str(s) for s in (data.get("subtasks") or [])],
        )


def update_plan_frontmatter(task_id: str, **fields) -> None:
    """Merge fields into plan.md's frontmatter, preserving the markdown body.
    Used by Phase 3 (selected_option) and Phase 4 (context_brief)."""
    path = _plan_path(task_id)
    body = ""
    data: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if m:
            data = yaml.safe_load(m.group(1)) or {}
            body = text[m.end():]
        else:
            body = text
    data.update(fields)
    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body.lstrip()}", encoding="utf-8")
