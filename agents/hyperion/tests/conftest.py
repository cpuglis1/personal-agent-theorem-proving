"""Shared pytest configuration for the Hyperion suite.

Adds the third test tier from the Lean-prover build plan §2: a ``lean`` marker for
integration tests that require a *real* Lean toolchain / the sidecar. The default
``uv run pytest`` stays hermetic and fast — ``lean``-marked tests are skipped unless
a Lean backend is available.

A "Lean backend is available" means EITHER a local toolchain (``lake`` on PATH) OR a
reachable verifier sidecar. The verifier (``verify_lean``) talks to the sidecar over
HTTP at ``settings.lean_url`` — it never shells out to local ``lake`` — so a gate that
only checks for ``lake`` wrongly skips the live tests when the sidecar is up but no
local toolchain is installed (the common Docker-stack case). We therefore also probe
``{settings.lean_url}/health``.

Also centralizes the ``anyio_backend`` fixture (the runner is asyncio-based) so new
test modules don't each redeclare it; existing modules that define it locally are
unaffected (a local fixture shadows this one).
"""

from __future__ import annotations

import shutil

import httpx
import pytest

from hyperion.config import settings

# Health probe must be cheap and total: collection runs on every `pytest` invocation,
# so a slow/raising probe would tax the hermetic default. Keep the timeout short.
_HEALTH_TIMEOUT_SECONDS = 1.0


def _sidecar_reachable() -> bool:
    """Return True iff the Lean verifier sidecar answers ``/health`` with HTTP 200.

    Fast and total: short timeout, and *all* exceptions (connect refused, DNS,
    timeout, malformed response) are swallowed and treated as unreachable. This is
    called during collection and must never raise.
    """
    try:
        resp = httpx.get(f"{settings.lean_url}/health", timeout=_HEALTH_TIMEOUT_SECONDS)
        return resp.status_code == 200
    except Exception:
        return False


def _lean_backend_available() -> bool:
    """A Lean backend is available if a local toolchain OR the sidecar is reachable."""
    return shutil.which("lake") is not None or _sidecar_reachable()


def pytest_configure(config):
    """Register the ``lean`` marker so it isn't reported as unknown."""
    config.addinivalue_line(
        "markers",
        "lean: integration test requiring a real Lean toolchain / sidecar "
        "(skipped unless `lake` is installed or the sidecar's /health is reachable)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip ``lean``-marked tests when no Lean backend (toolchain or sidecar) is up."""
    if _lean_backend_available():
        return
    skip_lean = pytest.mark.skip(
        reason="No Lean backend: `lake` not installed and sidecar /health unreachable"
    )
    for item in items:
        if "lean" in item.keywords:
            item.add_marker(skip_lean)


@pytest.fixture
def anyio_backend():
    """Run ``@pytest.mark.anyio`` tests on asyncio only (the runner is asyncio-based)."""
    return "asyncio"
