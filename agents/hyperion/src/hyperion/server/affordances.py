"""
Affordance schema (the implementation plan §4.5) — defined in Phase 0, populated in Phase 6.

An affordance is a structured request from an agent for human input, rendered by all
three surfaces (OWUI, Claude Code, web UI). Answers route through ``/approve`` (choices)
or ``/feedback`` (free text) — Phase 6 reuses the Phase 3 plumbing.

Role in the system
------------------
This module is purely a set of Pydantic data models — it contains no behavior beyond
validation and serialization. Agents and crews emit ``Affordance`` instances when a run
needs human input before it can continue (e.g. choosing a plan variant, confirming a
destructive step, or filling in missing parameters). The server attaches these to a run's
state and surfaces them to whichever client is observing the run. The client renders the
affordance, collects the human's response, and posts it back through the existing
``/approve`` (for ``choice`` / ``confirm`` selections) or ``/feedback`` (for ``form`` /
``question`` free-text) endpoints.

Key design decisions
--------------------
- A single ``Affordance`` model covers all four interaction types via the ``type``
  discriminator, so every surface only needs to understand one shape.
- ``options`` (for choices) and ``fields`` (for forms) coexist on the same model and default
  to empty lists; which one is populated depends on ``type``. This keeps the wire format
  stable regardless of interaction type.
- All models are JSON-serializable so the same payload can be sent unchanged to OWUI,
  Claude Code, and the web UI.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# The four supported interaction styles a human can be asked to resolve:
#   - "choice":   pick one of several discrete options (uses ``options``)
#   - "form":     fill in one or more typed fields (uses ``fields``)
#   - "question": answer a free-text prompt (no options/fields)
#   - "confirm":  yes/no acknowledgement (typically rendered as two options)
AffordanceType = Literal["choice", "form", "question", "confirm"]


class AffordanceOption(BaseModel):
    """A single selectable answer within a ``choice``/``confirm`` affordance.

    Attributes:
        id: Stable machine identifier for the option; this is the value the client
            sends back via ``/approve`` when the human selects it.
        label: Human-readable text shown to the user for this option.
        description: Optional longer explanation shown alongside the label.
            Defaults to an empty string.
    """

    id: str
    label: str
    description: str = ""


class AffordanceField(BaseModel):
    """A single input field within a ``form`` affordance.

    Attributes:
        id: Stable machine identifier for the field; keys the value the human
            supplies when the form is submitted.
        label: Human-readable prompt shown next to the input.
        type: Input widget/value kind. One of ``text``, ``number``, ``boolean``,
            or ``select``. Defaults to ``text``. When ``select``, the choices come
            from ``options``.
        options: Allowed values for a ``select`` field. Ignored for other types.
            Defaults to an empty list.
        required: Whether the human must provide a value before submitting.
            Defaults to ``False``.
    """

    id: str
    label: str
    type: Literal["text", "number", "boolean", "select"] = "text"
    options: list[str] = Field(default_factory=list)
    required: bool = False


class Affordance(BaseModel):
    """A structured request from an agent for human input during a run.

    A single model covers all interaction types; ``type`` discriminates which
    sub-payload (``options`` vs. ``fields``) is meaningful. The same instance is
    serialized and rendered identically by every client surface (OWUI, Claude
    Code, web UI), and the human's answer is posted back through ``/approve`` or
    ``/feedback``.

    Attributes:
        type: The interaction style (see ``AffordanceType``).
        prompt: The question/instruction text shown to the human.
        options: Choices for ``choice``/``confirm`` affordances. Empty otherwise.
            Defaults to an empty list.
        fields: Input fields for ``form`` affordances. Empty otherwise.
            Defaults to an empty list.
        agent_id: Identifier of the agent that raised the affordance, if known.
            Used by clients to attribute the request. Defaults to ``None``.
        stage: The workflow stage/step the run was in when the affordance was
            raised, if known. Defaults to ``None``.
    """

    type: AffordanceType
    prompt: str
    options: list[AffordanceOption] = Field(default_factory=list)
    fields: list[AffordanceField] = Field(default_factory=list)
    agent_id: Optional[str] = None
    stage: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this affordance to a plain JSON-compatible dict.

        Thin wrapper over Pydantic's ``model_dump`` provided as the canonical way
        to put an affordance onto the wire / into a run's state payload, keeping
        call sites decoupled from the underlying serialization method.

        Returns:
            A nested ``dict`` containing all fields (with ``options`` and
            ``fields`` expanded to lists of dicts) suitable for JSON encoding.
        """
        return self.model_dump()
