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


# ---------------------------------------------------------------------------
# Unquoted-colon recovery — the planner copies a request like "Prove in Lean
# 4: ..." verbatim into a scalar, which naive YAML rejects ("mapping values
# are not allowed here"). The shared loader quotes it instead of losing the plan.
# ---------------------------------------------------------------------------

_COLON_PLAN = """---
task_id: t_colon
original_request: Prove in Lean 4: for every natural number n, n - 0 = n
task_type: code
keywords: [nat, subtraction]
scaffold: |
  theorem nat_sub_zero (n : ℕ) : n - 0 = n := by
    simp
options:
  - id: a
    summary: direct
    subtasks:
      - id: direct
        description: close it
        lean_type: ∀ n, n - 0 = n
---
body prose
"""


def test_unquoted_colon_in_request_recovers_full_plan(tmp_path):
    """A colon-bearing top-level scalar is recovered, not fatal — plan stays intact."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_colon", _COLON_PLAN)
        plan = parse_plan("t_colon")  # previously raised yaml.YAMLError up the stack

    assert plan.task_type == "code"
    assert plan.keywords == ["nat", "subtraction"]
    # The colon-bearing request survives verbatim (block scalar + lean_type colon untouched).
    assert "n - 0 = n" in (plan.scaffold or "")
    assert plan.options and plan.options[0].subtasks[0].lean_type == "∀ n, n - 0 = n"


def test_update_frontmatter_survives_unquoted_colon(tmp_path):
    """The writer recovers the colon plan and preserves it through a merge (was a crash)."""
    from hyperion.crews.plan_contract import update_plan_frontmatter

    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_colon2", _COLON_PLAN)
        update_plan_frontmatter("t_colon2", selected_option="a")  # must not raise
        plan = parse_plan("t_colon2")

    assert plan.selected_option == "a"
    assert "n - 0 = n" in (plan.scaffold or "")  # existing plan preserved across the merge


def test_block_scalar_colons_are_not_mangled(tmp_path):
    """The sanitizer only touches top-level scalars — indented block content with
    colons (the scaffold body) must pass through byte-for-byte."""
    from hyperion.crews.plan_contract import _sanitize_frontmatter

    raw = (
        "original_request: a: b\n"
        "scaffold: |\n"
        "  theorem t (n : ℕ) : n = n := by\n"
        "    rfl\n"
    )
    out = _sanitize_frontmatter(raw)
    assert 'original_request: "a: b"' in out          # top-level scalar quoted
    assert "  theorem t (n : ℕ) : n = n := by" in out  # block body untouched


# ---------------------------------------------------------------------------
# keywords coercion — the planner emits keywords in shapes strict list[str]
# rejects (bare comma string, a bool from a True/False theorem, mixed list).
# A rejection used to hit the salvage path, which DROPS options — so the plan
# lost its sub-goals and the verified lemma never banked. Coercion keeps the
# validation green so options (and thus active_subtasks) survive.
# ---------------------------------------------------------------------------

_COMMA_KEYWORDS_PLAN = """---
task_id: t_kw
task_type: code
keywords: natural numbers, subtraction, induction
scaffold: |
  theorem t (n : ℕ) : n - 0 = n := by simp
options:
  - id: a
    summary: induct
    subtasks:
      - id: base
        description: base case
        lean_type: 0 - 0 = 0
---
body
"""


def test_comma_string_keywords_preserve_options(tmp_path):
    """A bare comma-string ``keywords`` is split, not fatal — so options survive."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_kw", _COMMA_KEYWORDS_PLAN)
        plan = parse_plan("t_kw")

    assert plan.keywords == ["natural numbers", "subtraction", "induction"]
    # The load-bearing assertion: options (and active_subtasks) are NOT lost.
    assert plan.options and plan.active_subtasks()[0].id == "base"


def test_keywords_coercion_handles_bool_and_mixed_list():
    """A YAML bool (from a True/False theorem) and bool/number list elements coerce to str."""
    from hyperion.crews.plan_contract import PlanFrontmatter

    assert PlanFrontmatter.model_validate({"keywords": True}).keywords == ["True"]
    assert PlanFrontmatter.model_validate({"keywords": ["a", False, 3]}).keywords == ["a", "False", "3"]
    assert PlanFrontmatter.model_validate({"keywords": None}).keywords == []
