"""Regression: CrewAI interpolates role/goal/backstory/description with
``str.format(**inputs)``. Any literal brace in record or user text (e.g. a YAML
schema example like ``{id, description}`` in the planner's goal) used to raise
``KeyError`` at ``crew.kickoff``. ``runner._esc`` doubles braces so the text
survives interpolation unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyperion.crews.runner import _esc

# The same inputs runner.py passes to crew.kickoff(inputs=...).
_INPUTS = {"task_id": "abc123", "request": "do the thing"}


def test_esc_survives_format_with_literal_braces():
    """``_esc`` round-trips literal-brace text through ``str.format`` unchanged.

    Asserts that the raw text breaks ``str.format`` (the historical
    ``KeyError``), but after ``_esc`` doubles the braces it survives
    interpolation and equals the original string.
    """
    raw = "Return a list of mappings: {id, description}"
    # Unescaped text raises the historical KeyError under CrewAI's interpolation.
    try:
        raw.format(**_INPUTS)
    except KeyError:
        pass
    else:
        raise AssertionError("expected literal braces to break str.format")
    # Escaped text round-trips back to the original.
    assert _esc(raw).format(**_INPUTS) == raw


def test_planner_goal_is_interpolation_safe():
    """The real planner agent's ``goal`` text is interpolation-safe via ``_esc``.

    Loads ``config/agents/planner.json`` (which contains a literal YAML schema
    example with braces) and asserts that ``_esc``'ing its ``goal`` lets it pass
    through ``str.format`` without raising and yields the original goal text.
    """
    record = json.loads(
        (Path(__file__).resolve().parents[1] / "config/agents/planner.json").read_text()
    )
    # _esc'd goal must not raise and must yield the original text.
    assert _esc(record["goal"]).format(**_INPUTS) == record["goal"]
