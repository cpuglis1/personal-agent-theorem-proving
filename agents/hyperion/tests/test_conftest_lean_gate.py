"""Unit tests for the ``lean``-marker collection gate in ``conftest``.

The gate must enable ``@pytest.mark.lean`` tests when EITHER a local toolchain
(``lake``) is on PATH OR the verifier sidecar's ``/health`` is reachable. These tests
monkeypatch the two probes so they run identically regardless of the host environment.
"""

from __future__ import annotations

import conftest


class _FakeItem:
    """Minimal stand-in for a pytest collection item."""

    def __init__(self, *, lean: bool):
        # ``"lean" in item.keywords`` is how the gate detects marked tests.
        self.keywords = {"lean": True} if lean else {}
        self.added_markers = []

    def add_marker(self, marker):
        self.added_markers.append(marker)


def _collect(monkeypatch, *, lake: bool, reachable: bool):
    """Run the gate against one lean + one non-lean item; return them."""
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/lake" if lake else None)
    monkeypatch.setattr(conftest, "_sidecar_reachable", lambda: reachable)
    lean_item = _FakeItem(lean=True)
    plain_item = _FakeItem(lean=False)
    conftest.pytest_collection_modifyitems(config=None, items=[lean_item, plain_item])
    return lean_item, plain_item


def test_lean_tests_collected_when_sidecar_reachable(monkeypatch):
    # No local toolchain, but the sidecar answers /health → lean tests run (no skip).
    lean_item, plain_item = _collect(monkeypatch, lake=False, reachable=True)
    assert lean_item.added_markers == []
    assert plain_item.added_markers == []


def test_lean_tests_skipped_when_no_lake_and_sidecar_unreachable(monkeypatch):
    # No toolchain and sidecar down → lean tests get a skip marker; others untouched.
    lean_item, plain_item = _collect(monkeypatch, lake=False, reachable=False)
    assert len(lean_item.added_markers) == 1
    assert plain_item.added_markers == []


def test_lean_tests_collected_when_lake_present(monkeypatch):
    # Local toolchain present, sidecar down → still enabled (the original behavior).
    lean_item, _ = _collect(monkeypatch, lake=True, reachable=False)
    assert lean_item.added_markers == []


def test_sidecar_reachable_is_total_on_network_error(monkeypatch):
    # A raising HTTP client must be swallowed → treated as unreachable, never raises.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(conftest.httpx, "get", _boom)
    assert conftest._sidecar_reachable() is False
