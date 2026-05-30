"""
The single planner output contract (PLAN_UNIFIED.md §4.2).

plan.md carries one YAML frontmatter schema that serves every consumer:
  - routing signals      task_type, keywords            (Phase 2)
  - HITL alternatives    options[], selected_option     (Phase 3)
  - auto context         context_brief                  (Phase 4)
  - critic trigger       needs_review                   (existing)

The parser is written ONCE here; the planner record's prompt is edited over time to
emit more of it. Missing task_type → "mixed"; missing options → one implicit option.
"""

from __future__ import annotations

import re
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from hyperion.config import settings

_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class Subtask(BaseModel):
    id: str
    description: str = ""


class PlanOption(BaseModel):
    id: str
    summary: str = ""
    subtasks: list[Subtask] = Field(default_factory=list)
    est_tool_calls: Optional[int] = None


class PlanFrontmatter(BaseModel):
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
        """Subtasks of the selected option, or the first option, or none."""
        if not self.options:
            return []
        chosen = None
        if self.selected_option:
            chosen = next((o for o in self.options if o.id == self.selected_option), None)
        return (chosen or self.options[0]).subtasks


def _plan_path(task_id: str):
    return settings.tasks_dir / task_id / "plan.md"


def parse_plan(task_id: str) -> PlanFrontmatter:
    """Parse plan.md frontmatter. Always returns a model — malformed/missing yields
    defaults (task_type='mixed') so the router degrades gracefully."""
    path = _plan_path(task_id)
    if not path.exists():
        return PlanFrontmatter()
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return PlanFrontmatter()
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return PlanFrontmatter()
    if not isinstance(data, dict):
        return PlanFrontmatter()
    # Normalize task_type to the known set.
    tt = str(data.get("task_type", "mixed")).strip().lower()
    if tt not in ("research", "code", "mixed"):
        tt = "mixed"
    data["task_type"] = tt
    try:
        return PlanFrontmatter.model_validate(data)
    except Exception:
        # Tolerate option shapes the planner hasn't learned yet.
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
