"""``_mock_lean`` — patch the Lean verifier so downstream tests run Lean-free.

The Phase 1 deliverable that lets every later phase (retrieval probe, verify
controller, abstractor re-check) run with NO Lean toolchain and NO httpx: it patches
:func:`hyperion.tools.lean_verify.verify_lean` to return canned ``LeanResult`` dicts.

Two modes:
  - a single fixed verdict (``ok`` / ``errors`` / ``elaborated_term`` / ``infra_ok``), or
  - ``results=[...]`` — a list of verdicts returned in order across successive calls,
    with the *last* one repeating. This is what the Phase 4 repair-loop tests use to
    script "fail, fail, …, finally pass" or "never converges" sequences.

``targets`` lists the fully-qualified ``verify_lean`` names to patch, so consumers
that did ``from ... import verify_lean`` (binding the name in their own module) are
also covered — pass e.g. ``("hyperion.crews.native.verify_lean",)`` once the verify
handler imports it. Defaults to the source module.

Importable as ``from lean_mock import mock_lean`` (alias ``_mock_lean``); pytest puts
the tests dir on ``sys.path`` (no ``__init__.py``, prepend import mode).
"""

from __future__ import annotations

import contextlib
from contextlib import ExitStack
from unittest.mock import patch

_DEFAULT_TARGETS = ("hyperion.tools.lean_verify.verify_lean",)


@contextlib.contextmanager
def mock_lean(
    *,
    ok: bool = True,
    errors: list[str] | None = None,
    elaborated_term: str | None = None,
    infra_ok: bool = True,
    results: list[dict] | None = None,
    targets: tuple[str, ...] | list[str] | None = None,
):
    """Patch ``verify_lean`` to return canned verdict(s). See module docstring.

    Yields the primary mock so a test can assert call count / arguments (e.g. that
    the verify controller called the oracle exactly N times).
    """
    seq = [dict(r) for r in results] if results is not None else None

    def _fake(source, *, mode="full", profile="core", timeout=None):
        if seq is not None:
            # Pop until one remains, then repeat the last verdict indefinitely.
            chosen = seq.pop(0) if len(seq) > 1 else seq[0]
            out = {"ok": True, "errors": [], "elaborated_term": None, "infra_ok": True}
            out.update(chosen)
            out["mode"] = mode
            out["profile"] = profile
            out["errors"] = list(out.get("errors") or [])
            return out
        return {
            "ok": ok,
            "errors": list(errors or []),
            "elaborated_term": elaborated_term,
            "mode": mode,
            "profile": profile,
            "infra_ok": infra_ok,
        }

    patch_targets = tuple(targets) if targets else _DEFAULT_TARGETS
    with ExitStack() as stack:
        mocks = [stack.enter_context(patch(t, side_effect=_fake)) for t in patch_targets]
        yield mocks[0]


# Alias matching the build-plan's naming.
_mock_lean = mock_lean
