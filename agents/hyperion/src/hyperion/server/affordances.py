"""
Affordance schema (PLAN_UNIFIED.md §4.5) — defined in Phase 0, populated in Phase 6.

An affordance is a structured request from an agent for human input, rendered by all
three surfaces (OWUI, Claude Code, web UI). Answers route through ``/approve`` (choices)
or ``/feedback`` (free text) — Phase 6 reuses the Phase 3 plumbing.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

AffordanceType = Literal["choice", "form", "question", "confirm"]


class AffordanceOption(BaseModel):
    id: str
    label: str
    description: str = ""


class AffordanceField(BaseModel):
    id: str
    label: str
    type: Literal["text", "number", "boolean", "select"] = "text"
    options: list[str] = Field(default_factory=list)
    required: bool = False


class Affordance(BaseModel):
    type: AffordanceType
    prompt: str
    options: list[AffordanceOption] = Field(default_factory=list)
    fields: list[AffordanceField] = Field(default_factory=list)
    agent_id: Optional[str] = None
    stage: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()
