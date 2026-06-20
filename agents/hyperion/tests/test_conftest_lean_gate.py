"""Unit tests for the ``lean``-marker collection gate in ``conftest``.

The gate must enable ``@pytest.mark.lean`` tests only when the verifier sidecar can
actually verify a trivial theorem. A live ``/health`` endpoint is not enough: the
FastAPI process can be up while ``/verify`` is broken.
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


def _collect(monkeypatch, *, verifies: bool):
    """Run the gate against one lean + one non-lean item; return them."""
    monkeypatch.setattr(conftest, "_sidecar_verifies", lambda: verifies)
    lean_item = _FakeItem(lean=True)
    plain_item = _FakeItem(lean=False)
    conftest.pytest_collection_modifyitems(config=None, items=[lean_item, plain_item])
    return lean_item, plain_item


def test_lean_tests_collected_when_sidecar_verifies(monkeypatch):
    # The sidecar accepts a trivial theorem → live Lean tests run.
    lean_item, plain_item = _collect(monkeypatch, verifies=True)
    assert lean_item.added_markers == []
    assert plain_item.added_markers == []


def test_lean_tests_skipped_when_sidecar_cannot_verify(monkeypatch):
    # Sidecar down or unable to run Lean → lean tests get a skip marker; others untouched.
    lean_item, plain_item = _collect(monkeypatch, verifies=False)
    assert len(lean_item.added_markers) == 1
    assert plain_item.added_markers == []


def test_lean_backend_available_uses_verification_probe(monkeypatch):
    monkeypatch.setattr(conftest, "_sidecar_verifies", lambda: True)
    assert conftest._lean_backend_available() is True
    monkeypatch.setattr(conftest, "_sidecar_verifies", lambda: False)
    assert conftest._lean_backend_available() is False


def test_sidecar_verifies_is_total_on_network_error(monkeypatch):
    # A raising HTTP client must be swallowed → treated as unreachable, never raises.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(conftest.httpx, "post", _boom)
    assert conftest._sidecar_verifies() is False


def test_sidecar_verifies_rejects_health_only_or_failed_verify(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": False, "errors": ["lean sidecar exception"]}

    monkeypatch.setattr(conftest.httpx, "post", lambda *_args, **_kwargs: _Resp())
    assert conftest._sidecar_verifies() is False
