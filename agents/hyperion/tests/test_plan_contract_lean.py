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


_CLOSER_PLAN = """---
task_id: t_closer
task_type: code
selected_option: a
options:
  - id: a
    summary: rewrite chain
    closer: exact h1.trans h2
    subtasks:
      - id: h1
        description: expand
        lean_type: "a = b"
      - id: h2
        description: simplify
        lean_type: "b = c"
---
narrative
"""


def test_active_closer_reads_option_closer(tmp_path):
    """The decomposer's composing tactic is exposed via ``active_closer`` for the
    active option; a plan without one returns ``None`` (heuristic closer applies)."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_closer", _CLOSER_PLAN)
        plan = parse_plan("t_closer")
        _write_plan(tmp_path, "t1", _PROVER_PLAN)
        plan_no_closer = parse_plan("t1")

    assert plan.active_closer() == "exact h1.trans h2"
    assert plan_no_closer.active_closer() is None


def test_recovers_active_subtasks_from_scaffold_when_options_missing(tmp_path):
    """A scaffold-only prover plan still fans out over its typed ``have`` holes."""
    scaffold_only = """---
task_type: code
scaffold: "example : 2 ^ 3 + 1 = 9 := by\\n  have h1 : 2 ^ 3 = 8 := sorry,\\n  have h2 : 8 + 1 = 9 := sorry,\\n  exact h2 ▸ h1"
---
body
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_scaffold_only", scaffold_only)
        plan = parse_plan("t_scaffold_only")

    subs = plan.active_subtasks()
    assert [s.id for s in subs] == ["h1", "h2"]
    assert [s.lean_type for s in subs] == ["2 ^ 3 = 8", "8 + 1 = 9"]


def test_string_literal_lean_type_scalars_are_recovered(tmp_path):
    """Lean string expressions can look partly quoted to YAML but must parse as one scalar."""
    string_plan = """---
task_type: code
scaffold: |
  example : ("ab" ++ "cd" = "abcd") ∧ ("x" ++ "yz" = "xyz") := by
    have h1 : "ab" ++ "cd" = "abcd" := rfl;
    have h2 : "x" ++ "yz" = "xyz" := rfl;
    exact ⟨h1, h2⟩
options:
  - id: a
    summary: string conjunction
    subtasks:
      - id: h1
        description: first concatenation
        lean_type: "ab" ++ "cd" = "abcd"
      - id: h2
        description: second concatenation
        lean_type: "x" ++ "yz" = "xyz"
---
body
"""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_string_literals", string_plan)
        plan = parse_plan("t_string_literals")

    assert plan.scaffold is not None
    assert [s.lean_type for s in plan.active_subtasks()] == [
        '"ab" ++ "cd" = "abcd"',
        '"x" ++ "yz" = "xyz"',
    ]


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
    """The sanitizer touches scalar values but never block-scalar bodies — indented
    block content with colons (the scaffold body) must pass through byte-for-byte."""
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


# A real live failure (task 8f3a7726): a *nested* option ``summary`` carried an unquoted
# colon ("two steps: subtraction and addition"), which the column-0-only sanitizer missed,
# so the whole frontmatter failed to parse → options dropped → no per-sub-goal fan-out.
_NESTED_COLON_PLAN = """---
task_id: 1
task_type: code
scaffold: |
  example : 18 - 7 + 4 = 15 := by
    have h1 : 18 - 7 = 11 := sorry
    have h2 : 11 + 4 = 15 := sorry
    exact h2
options:
  - id: a
    summary: Break the arithmetic into two steps: subtraction and addition.
    subtasks:
      - id: h1
        description: Prove that 18 minus 7 equals 11.
        lean_type: 18 - 7 = 11
      - id: h2
        description: Prove that 11 plus 4 equals 15.
        lean_type: 11 + 4 = 15
---
prose
"""


def test_nested_option_summary_colon_recovers_subtasks(tmp_path):
    """An unquoted colon in a nested option ``summary`` no longer sinks the whole plan:
    the multi-sub-goal options survive so the runner can fan the prover chain out."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_nested", _NESTED_COLON_PLAN)
        plan = parse_plan("t_nested")

    assert [s.id for s in plan.active_subtasks()] == ["h1", "h2"]
    assert plan.options[0].subtasks[1].lean_type == "11 + 4 = 15"
    # Block-scalar scaffold body (its own ``have h : T`` colons) survives intact.
    assert "have h2 : 11 + 4 = 15 := sorry" in (plan.scaffold or "")


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


# ---------------------------------------------------------------------------
# unclosed frontmatter fence — the planner LLM routinely emits the whole plan as a
# leading ``---`` block and forgets the closing ``---``. The strict frontmatter regex
# then never matched, so parse_plan returned bare defaults: empty active_subtasks ->
# the prover ran on sub-goal "0" with goal_type = the raw prose request, and Path-A
# lemma retrieval (which needs the clean Lean type) always missed. Recovery keeps the
# typed plan so the live snowball (instance goal reuses a banked ∀-lemma) can close.
# ---------------------------------------------------------------------------

_UNCLOSED_PLAN = """---
task_id: t_unclosed
task_type: code
keywords: [addition, identity]
scaffold: |
  have step1 : 0 + 7 = 7 := sorry
  exact step1
options:
  - id: 'a'
    summary: direct application of zero-add identity
    subtasks:
      - id: 'step1'
        description: show 0 + 7 = 7
        lean_type: 0 + 7 = 7
"""


def test_unclosed_frontmatter_fence_recovers_typed_plan(tmp_path):
    """A plan with an opening ``---`` but NO closing fence still yields its typed
    sub-goals — the exact failure that degraded the prover to the prose request."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_unclosed", _UNCLOSED_PLAN)
        plan = parse_plan("t_unclosed")

    assert plan.task_type == "code"
    assert plan.options, "options must survive an unclosed fence (was dropped -> defaults)"
    subs = plan.active_subtasks()
    assert subs and subs[0].id == "step1"
    # The load-bearing field: the clean Lean type drives Path-A retrieval, not the prose.
    assert subs[0].lean_type == "0 + 7 = 7"


_INT_TASK_ID_PLAN = """---
task_id: 1
original_request: Prove that 0 + 7 = 7.
task_type: code
keywords: [addition, identity]
options:
  - id: 'a'
    summary: direct
    subtasks:
      - id: 'step1'
        description: show 0 + 7 = 7
        lean_type: 0 + 7 = 7
"""


def test_int_task_id_does_not_drop_options(tmp_path):
    """``task_id: 1`` (YAML int) must coerce to str, not trip validation and lose options —
    the planner echoes the numeric task id and strict ``Optional[str]`` would reject it."""
    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_intid", _INT_TASK_ID_PLAN)
        plan = parse_plan("t_intid")

    assert plan.task_id == "1"  # coerced, not dropped
    assert plan.options and plan.active_subtasks()[0].lean_type == "0 + 7 = 7"


def test_update_frontmatter_recloses_unclosed_fence(tmp_path):
    """Writing to an unclosed-fence plan re-emits a properly fenced block, preserving
    the typed plan across the merge (rather than wrapping the whole file as a body)."""
    from hyperion.crews.plan_contract import update_plan_frontmatter

    with patch.object(settings, "tasks_dir", tmp_path):
        _write_plan(tmp_path, "t_unclosed2", _UNCLOSED_PLAN)
        update_plan_frontmatter("t_unclosed2", selected_option="a")  # must not corrupt
        plan = parse_plan("t_unclosed2")

    assert plan.selected_option == "a"
    assert plan.active_subtasks()[0].lean_type == "0 + 7 = 7"  # typed plan preserved


def test_keywords_coercion_handles_bool_and_mixed_list():
    """A YAML bool (from a True/False theorem) and bool/number list elements coerce to str."""
    from hyperion.crews.plan_contract import PlanFrontmatter

    assert PlanFrontmatter.model_validate({"keywords": True}).keywords == ["True"]
    assert PlanFrontmatter.model_validate({"keywords": ["a", False, 3]}).keywords == ["a", "False", "3"]
    assert PlanFrontmatter.model_validate({"keywords": None}).keywords == []
