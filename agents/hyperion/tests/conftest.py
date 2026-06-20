"""Shared pytest configuration for the Hyperion suite.

Adds the third test tier from the Lean-prover build plan §2: a ``lean`` marker for
integration tests that require a *working* Lean verifier. The default ``uv run pytest``
stays hermetic and fast — ``lean``-marked tests are skipped unless a verifier can
actually accept a trivial theorem.

A "Lean backend is available" means the sidecar can run Lean, not merely that its
FastAPI process is alive. A previous gate accepted ``/health`` alone, so a broken
sidecar returning HTTP 500 from ``/verify`` still ran the live tests and failed them
as code regressions. We now probe ``/verify`` with ``theorem t : True := trivial``.

Also centralizes the ``anyio_backend`` fixture (the runner is asyncio-based) so new
test modules don't each redeclare it; existing modules that define it locally are
unaffected (a local fixture shadows this one).
"""

from __future__ import annotations

import httpx
import pytest

from hyperion.config import settings

# Probe must be cheap and total: collection runs on every `pytest` invocation, so a
# slow/raising probe would tax the hermetic default. Keep the timeout short.
_VERIFY_TIMEOUT_SECONDS = 2.0


def _sidecar_verifies() -> bool:
    """Return True iff the Lean verifier accepts a trivial theorem.

    Fast and total: short timeout, and *all* exceptions (connect refused, DNS,
    timeout, malformed response) are swallowed and treated as unreachable. This is
    called during collection and must never raise.
    """
    try:
        resp = httpx.post(
            f"{settings.lean_url}/verify",
            json={"source": "theorem t : True := trivial", "mode": "full"},
            timeout=_VERIFY_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data, dict) and data.get("ok") is True
    except Exception:
        return False


def _lean_backend_available() -> bool:
    """A Lean backend is available only when the sidecar can verify Lean."""
    return _sidecar_verifies()


def pytest_configure(config):
    """Register the ``lean`` marker so it isn't reported as unknown."""
    config.addinivalue_line(
        "markers",
        "lean: integration test requiring a real Lean toolchain / sidecar "
        "(skipped unless `lake` is installed or the sidecar's /health is reachable)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip ``lean``-marked tests when no working Lean verifier is up."""
    if _lean_backend_available():
        return
    skip_lean = pytest.mark.skip(
        reason="No working Lean verifier: sidecar /verify did not accept a trivial theorem"
    )
    for item in items:
        if "lean" in item.keywords:
            item.add_marker(skip_lean)


@pytest.fixture
def anyio_backend():
    """Run ``@pytest.mark.anyio`` tests on asyncio only (the runner is asyncio-based)."""
    return "asyncio"
