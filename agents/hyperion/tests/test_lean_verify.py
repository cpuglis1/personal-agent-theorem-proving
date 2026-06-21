"""Tests for the Lean verifier tool (build-plan Phase 1).

Two tiers:
  - Unit (offline): the sidecar HTTP is mocked (``httpx.post``). Asserts the verdict
    parsing and — the load-bearing distinction — that an unreachable/garbage verifier
    degrades to ``infra_ok=False`` (retryable), NEVER a false ``ok=False``.
  - Integration (``@pytest.mark.lean``): a *real* toolchain/sidecar. Skipped unless
    ``lake`` is installed (see conftest). Asserts a known-true theorem verifies, a
    broken proof fails with a real diagnostic, skeleton-vs-full ``sorry`` handling,
    and records warm round-trip latency for Post-work cap tuning.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from hyperion.tools.lean_verify import LeanVerifyTool, lean_axioms, verify_lean


def _resp(payload):
    """A fake httpx.Response with ``.raise_for_status()`` and ``.json()``."""
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value=payload)
    return m


# ---------------------------------------------------------------------------
# Unit (offline) — mock the sidecar HTTP
# ---------------------------------------------------------------------------


def test_passing_proof_returns_ok():
    payload = {"ok": True, "errors": [], "elaborated_term": "trivial"}
    with patch("httpx.post", return_value=_resp(payload)):
        res = verify_lean("theorem t : True := trivial", mode="full")
    assert res["ok"] is True
    assert res["infra_ok"] is True
    assert res["errors"] == []
    assert res["elaborated_term"] == "trivial"
    assert res["mode"] == "full"


def test_type_error_returns_parsed_errors():
    payload = {"ok": False, "errors": ["type mismatch: expected Nat, got Bool"]}
    with patch("httpx.post", return_value=_resp(payload)):
        res = verify_lean("theorem t : Nat := true", mode="full")
    assert res["ok"] is False
    assert res["infra_ok"] is True  # a real verdict, not an infra failure
    assert res["errors"] == ["type mismatch: expected Nat, got Bool"]


def test_service_down_degrades_distinctly_not_false_ok():
    """The load-bearing distinction: a verifier outage is a retryable infra signal,
    never conflated with a proof failure."""
    with patch("httpx.post", side_effect=Exception("connection refused")):
        res = verify_lean("theorem t : True := trivial", mode="full")
    # infra_ok=False marks it retryable; a caller keying on infra_ok never reads this
    # as a ground-truth ok=False verdict.
    assert res["infra_ok"] is False
    assert res["ok"] is False
    assert any("unavailable" in e for e in res["errors"])


def test_malformed_payload_is_infra_failure():
    """A 200 with a body missing a bool ``ok`` is malformed → infra failure, not a verdict."""
    with patch("httpx.post", return_value=_resp({"unexpected": 1})):
        res = verify_lean("whatever", mode="full")
    assert res["infra_ok"] is False
    assert res["ok"] is False


def test_mode_is_forwarded_and_echoed():
    captured = {}

    def _capture(url, json, timeout):  # noqa: A002 - mirror httpx.post kwarg name
        captured["url"] = url
        captured["json"] = json
        return _resp({"ok": True, "errors": []})

    with patch("httpx.post", side_effect=_capture):
        res = verify_lean("src", mode="skeleton")
    assert captured["json"]["mode"] == "skeleton"
    assert res["mode"] == "skeleton"


def test_tool_wrapper_ok_string():
    with patch("httpx.post", return_value=_resp({"ok": True, "errors": [], "elaborated_term": "t"})):
        out = LeanVerifyTool()._run("theorem t : True := trivial")
    assert out.startswith("OK")
    assert "elaborated_term: t" in out


def test_tool_wrapper_failed_string():
    with patch("httpx.post", return_value=_resp({"ok": False, "errors": ["boom"]})):
        out = LeanVerifyTool()._run("bad", mode="full")
    assert out.startswith("FAILED:")
    assert "boom" in out


def test_tool_wrapper_unavailable_string():
    with patch("httpx.post", side_effect=Exception("down")):
        out = LeanVerifyTool()._run("src")
    assert out.startswith("VERIFIER_UNAVAILABLE")


def test_tool_wrapper_coerces_bad_mode_to_full():
    captured = {}

    def _capture(url, json, timeout):  # noqa: A002
        captured["json"] = json
        return _resp({"ok": True, "errors": []})

    with patch("httpx.post", side_effect=_capture):
        LeanVerifyTool()._run("src", mode="bogus")
    assert captured["json"]["mode"] == "full"


# ---------------------------------------------------------------------------
# Unit (offline) — lean_axioms client (the #print axioms soundness chokepoint)
# ---------------------------------------------------------------------------


def test_axioms_parses_dependency_list():
    payload = {"ok": True, "axioms": ["propext", "Classical.choice", "Quot.sound"], "errors": []}
    with patch("httpx.post", return_value=_resp(payload)):
        res = lean_axioms("theorem t : True := trivial", "t")
    assert res["ok"] is True
    assert res["infra_ok"] is True
    assert res["axioms"] == ["propext", "Classical.choice", "Quot.sound"]


def test_axioms_empty_list_is_a_real_verdict():
    payload = {"ok": True, "axioms": [], "errors": []}
    with patch("httpx.post", return_value=_resp(payload)):
        res = lean_axioms("theorem t : True := trivial", "t")
    assert res["ok"] is True and res["infra_ok"] is True and res["axioms"] == []


def test_axioms_sorryax_surfaces_in_list():
    payload = {"ok": True, "axioms": ["sorryAx"], "errors": []}
    with patch("httpx.post", return_value=_resp(payload)):
        res = lean_axioms("theorem t : True := by sorry", "t")
    assert res["ok"] is True
    assert res["axioms"] == ["sorryAx"]  # interpretation (reject) is soundness.py's job


def test_axioms_elaboration_failure_is_not_clean():
    payload = {"ok": False, "axioms": [], "errors": ["unknown identifier 't'"]}
    with patch("httpx.post", return_value=_resp(payload)):
        res = lean_axioms("nonsense", "t")
    assert res["infra_ok"] is True
    assert res["ok"] is False
    assert res["errors"] == ["unknown identifier 't'"]


def test_axioms_service_down_degrades_distinctly():
    with patch("httpx.post", side_effect=Exception("connection refused")):
        res = lean_axioms("theorem t : True := trivial", "t")
    assert res["infra_ok"] is False
    assert res["ok"] is False
    assert any("unavailable" in e for e in res["errors"])


def test_axioms_malformed_payload_is_infra_failure():
    with patch("httpx.post", return_value=_resp({"axioms": []})):  # missing bool ok
        res = lean_axioms("whatever", "t")
    assert res["infra_ok"] is False
    assert res["ok"] is False


def test_axioms_forwards_decl():
    captured = {}

    def _capture(url, json, timeout):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        return _resp({"ok": True, "axioms": [], "errors": []})

    with patch("httpx.post", side_effect=_capture):
        lean_axioms("src", "MyThm")
    assert captured["url"].endswith("/axioms")
    assert captured["json"] == {"source": "src", "decl": "MyThm"}


# ---------------------------------------------------------------------------
# Integration (live Lean) — real sidecar; skipped unless `lake` is installed
# ---------------------------------------------------------------------------


@pytest.mark.lean
def test_real_true_theorem_verifies():
    res = verify_lean("theorem t : True := trivial", mode="full")
    assert res["infra_ok"] is True, "Lean sidecar must be reachable for the lean tier"
    assert res["ok"] is True


@pytest.mark.lean
def test_real_broken_proof_fails_with_diagnostic():
    res = verify_lean("theorem t : 1 = 2 := rfl", mode="full")
    assert res["infra_ok"] is True
    assert res["ok"] is False
    assert res["errors"], "a broken proof must produce at least one diagnostic"


@pytest.mark.lean
def test_real_sorry_skeleton_vs_full():
    src = "theorem t : True := by sorry"
    skeleton = verify_lean(src, mode="skeleton")
    full = verify_lean(src, mode="full")
    assert skeleton["infra_ok"] and full["infra_ok"]
    assert skeleton["ok"] is True, "skeleton mode permits sorry"
    assert full["ok"] is False, "full mode forbids sorry"


@pytest.mark.lean
def test_real_warm_cache_latency_recorded(capsys):
    start = time.perf_counter()
    res = verify_lean("theorem t : True := trivial", mode="full")
    elapsed = time.perf_counter() - start
    assert res["infra_ok"] is True
    # Surfaced for Post-work cap tuning (run with -s to see it).
    print(f"[lean-latency] warm full verify round-trip: {elapsed*1000:.1f} ms")


@pytest.mark.lean
def test_real_axioms_clean_proof():
    res = lean_axioms("theorem t : True := trivial", "t")
    assert res["infra_ok"] is True, "Lean sidecar must be reachable for the lean tier"
    assert res["ok"] is True
    # A trivial proof depends on no axioms (or only the sound base).
    from hyperion.crews.soundness import axioms_clean

    assert axioms_clean(res["axioms"], strict=True)


@pytest.mark.lean
def test_real_axioms_reports_sorryax_for_gap():
    res = lean_axioms("theorem t : True := by sorry", "t")
    assert res["infra_ok"] is True
    assert res["ok"] is True
    assert "sorryAx" in res["axioms"], "an unclosed hole must surface as sorryAx"
