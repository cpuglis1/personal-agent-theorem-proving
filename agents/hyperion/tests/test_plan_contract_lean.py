"""Plan-contract prover extension (build plan Phase 4, deliverable 1).

What is under test:
  - ``Subtask.lean_type`` and ``PlanFrontmatter.scaffold`` parse out of a prover plan.
  - The parser stays **tolerant** — the load-bearing property for the runner: an old
    plan with no ``lean_type``/``scaffold`` still validates (defaults), a partial plan
    degrades, and malformed YAML never raises out of ``parse_plan``.
  - The salvage path (option shapes the planner hasn't learned) still recovers
    ``scaffold`` rather than losing it.

Mirrors the existing planner-contract tests: ``settings.tasks_dir`` is patched to
``tmp_path`` and plan.md is written by hand, so no LLM/disk-state leaks in.
"""

from __future__ import annotations

from unittest.mock import patch

from hyperion.config import settings
from hyperion.crews.plan_contract import parse_plan


def _write_plan(tasks_dir, task_id: str, text: str) -> None:
    d = tasks_dir / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.md").write_text(text, encoding="utf-8")


_PROVER_PLAN = """---
task_id: t1
task_type: code
scaffold: |
  theorem target : P ∧ Q := by
    have h1 : P := sorry
    have h2 : Q := sorry
    exact ⟨h1, h2⟩
options:
  - id: a
    summary: split the conjunction
    subtasks:
      - id: h1
        description: prove P
        lean_type: "P"
      - id: h2
        description: prove Q
        lean_type: "Q"
---

# Plan narrative
"""


def test_reads_lean_type_and_scaffold(tmp_path):
    """A prover plan exposes per-subtask ``lean_type`` and the have-chain ``scaffold``."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t1", _PROVER_PLAN)
        plan = parse_plan("t1")

    assert plan.scaffold is not None
    assert "have h1 : P := sorry" in plan.scaffold
    subs = plan.active_subtasks()
    assert [s.id for s in subs] == ["h1", "h2"]
    assert [s.lean_type for s in subs] == ["P", "Q"]


def test_old_plan_without_lean_type_or_scaffold_still_validates(tmp_path):
    """A pre-prover plan (no lean_type, no scaffold) parses with safe defaults."""
    legacy = """---
task_type: research
options:
  - id: a
    summary: do the thing
    subtasks:
      - id: s1
        description: a step
---
body
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t2", legacy)
        plan = parse_plan("t2")

    assert plan.scaffold is None
    subs = plan.active_subtasks()
    assert subs[0].lean_type == ""  # defaulted, not raised


def test_malformed_yaml_never_raises(tmp_path):
    """Broken frontmatter degrades to defaults rather than propagating an error."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t3", "---\n: : not: valid: yaml: [\n---\nbody")
        plan = parse_plan("t3")  # must not raise
    assert plan.task_type == "mixed"
    assert plan.scaffold is None


def test_salvage_path_preserves_scaffold(tmp_path):
    """When an option shape fails validation, the salvage path still recovers the
    scaffold (and other cheap fields) instead of dropping the whole plan."""
    # `options` is a scalar, not a list of mappings → model_validate raises → salvage.
    bad_options = """---
task_type: code
scaffold: "theorem t : True := by sorry"
keywords: [a, b]
options: "not-a-list"
---
body
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t4", bad_options)
        plan = parse_plan("t4")  # must not raise

    assert plan.scaffold == "theorem t : True := by sorry"
    assert plan.keywords == ["a", "b"]
    assert plan.options == []  # the unparseable part was dropped
