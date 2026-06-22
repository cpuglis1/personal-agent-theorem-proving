from __future__ import annotations

from hyperion.crews.lean_statement import parse_formal_statement


def test_parse_formal_statement_with_import_preamble_and_binder():
    src = """import Mathlib

open Real Nat Topology
open BigOperators

theorem mathd_algebra_182
  (y : ℂ) :
  7 * (3 * y + 2) = 21 * y + 14 := by
  sorry"""

    out = parse_formal_statement(src)

    assert out is not None
    assert out.preamble.startswith("import Mathlib")
    assert out.header == "theorem mathd_algebra_182\n  (y : ℂ)"
    assert out.goal == "7 * (3 * y + 2) = 21 * y + 14"
    assert out.local_context[0].names == ["y"]
    assert out.local_context[0].type == "ℂ"


def test_parse_formal_statement_multi_binder_and_hypothesis_args():
    src = """theorem foo (x y : ℝ) (h : 0 < x) : x + y = y + x := by
  sorry"""

    out = parse_formal_statement(src)

    assert out is not None
    assert out.preamble == ""
    assert out.header == "theorem foo (x y : ℝ) (h : 0 < x)"
    assert out.goal == "x + y = y + x"
    assert [(b.names, b.type) for b in out.local_context] == [
        (["x", "y"], "ℝ"),
        (["h"], "0 < x"),
    ]


def test_parse_formal_statement_no_imports_example():
    src = """example : 2004 % 12 = 0 := by
  sorry"""

    out = parse_formal_statement(src)

    assert out is not None
    assert out.preamble == ""
    assert out.header == "example"
    assert out.goal == "2004 % 12 = 0"
    assert out.local_context == []

