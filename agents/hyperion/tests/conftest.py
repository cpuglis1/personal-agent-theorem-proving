"""Shared pytest configuration for the Hyperion suite.

Adds the third test tier from the Lean-prover build plan §2: a ``lean`` marker for
integration tests that require a *real* Lean toolchain / the sidecar. The default
``uv run pytest`` stays hermetic and fast — ``lean``-marked tests are skipped unless
``lake`` is on PATH, so they only run nightly / on-demand against a real toolchain.

Also centralizes the ``anyio_backend`` fixture (the runner is asyncio-based) so new
test modules don't each redeclare it; existing modules that define it locally are
unaffected (a local fixture shadows this one).
"""

from __future__ import annotations

import shutil

import pytest


def pytest_configure(config):
    """Register the ``lean`` marker so it isn't reported as unknown."""
    config.addinivalue_line(
        "markers",
        "lean: integration test requiring a real Lean toolchain / sidecar "
        "(skipped unless `lake` is installed)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip ``lean``-marked tests when no Lean toolchain (`lake`) is installed."""
    if shutil.which("lake") is not None:
        return
    skip_lean = pytest.mark.skip(reason="Lean toolchain (lake) not installed")
    for item in items:
        if "lean" in item.keywords:
            item.add_marker(skip_lean)


@pytest.fixture
def anyio_backend():
    """Run ``@pytest.mark.anyio`` tests on asyncio only (the runner is asyncio-based)."""
    return "asyncio"
