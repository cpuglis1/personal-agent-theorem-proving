"""Lean verifier tool — the prover's oracle.

Role in the system
------------------
Submits a candidate Lean 4 source string to the Lean sidecar (a long-lived service
on ``ai-net`` with a warm Mathlib cache; see ``docker-compose.lean.yml``) and returns
a structured verdict. This *replaces the LLM-critic notion of "verification" with a
real kernel oracle*: the model proposes, the kernel judges, and a proposal is checked
on the very next line. An LLM can be arbitrarily creative and still cannot hallucinate
a pass.

Two surfaces, per the build plan §1:
  - :func:`verify_lean` — a plain function that native nodes (verify/retrieve probe)
    call directly, with no ReAct loop in the way. This is the hot path.
  - :class:`LeanVerifyTool` — a thin CrewAI ``BaseTool`` wrapper so an agent (e.g. a
    repair agent that owns its own inner loop) can call the verifier as a tool.

Key design decision: infra-down ≠ proof-failure (load-bearing)
--------------------------------------------------------------
These two outcomes must NEVER be conflated:
  - **The proof doesn't type-check** → ``ok=False, infra_ok=True`` with parsed
    ``errors``. A real, ground-truth verdict.
  - **The verifier service is unreachable / errored / returned garbage** → a
    *retryable infra signal* ``infra_ok=False`` (never a false ``ok=False``).

Callers route on ``infra_ok`` first (retry/degrade), then trust ``ok``. This mirrors
the fail-soft degrade in ``tools/reranker.py`` but keeps the distinction explicit,
because here the verdict is load-bearing ground truth, not a nice-to-have.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional, TypedDict

import httpx
from crewai.tools import BaseTool

from hyperion.config import settings

logger = logging.getLogger(__name__)

VerifyMode = Literal["skeleton", "full"]
LeanProfile = Literal["core", "mathlib"]

# Lean elaboration can take a while even with a warm Mathlib cache; budget generously.
# (Post-work re-tunes caps against the latency measured by the Phase 1 integration test.)
_TIMEOUT_SECONDS = 120.0


class LeanResult(TypedDict):
    """The verifier's structured verdict.

    Attributes:
        ok: True iff Lean accepts ``source`` in the given mode. Ground truth — only
            ever produced by the kernel, never by an LLM. Meaningful only when
            ``infra_ok`` is True.
        errors: Parsed compiler diagnostics (empty when ``ok``). On infra failure,
            carries the reason the verifier was unreachable.
        elaborated_term: The elaborated proof term when the verifier reports one,
            else None.
        mode: The mode the source was checked in. ``skeleton`` permits ``sorry``
            (the have-chain must compose to the target — powers P1); ``full`` forbids
            ``sorry`` (the proof must close — powers P3/P4).
        infra_ok: False ⇒ the verifier service was unreachable/errored (retryable);
            the verdict is meaningless. True ⇒ ``ok``/``errors`` are a real verdict.
    """

    ok: bool
    errors: list[str]
    elaborated_term: Optional[str]
    mode: VerifyMode
    infra_ok: bool


def _infra_unavailable(mode: VerifyMode, reason: object) -> LeanResult:
    """Build the retryable infra-down result (``infra_ok=False``, never a real verdict)."""
    return {
        "ok": False,
        "errors": [f"lean verifier unavailable: {reason}"],
        "elaborated_term": None,
        "mode": mode,
        "infra_ok": False,
    }


def verify_lean(
    source: str,
    *,
    mode: VerifyMode = "full",
    profile: LeanProfile | None = None,
    timeout: float | None = None,
) -> LeanResult:
    """Type-check ``source`` against the Lean sidecar and return a :class:`LeanResult`.

    Args:
        source: The full Lean 4 source to verify.
        mode: ``"skeleton"`` (``sorry`` permitted; checks the scaffold composes) or
            ``"full"`` (``sorry`` forbidden; the proof must close). Defaults to
            ``"full"``.
        timeout: Optional per-call HTTP timeout in seconds (defaults to
            ``_TIMEOUT_SECONDS``).

    Returns:
        A :class:`LeanResult`. **Fail-soft on infra, hard-fail on Lean:** a sidecar
        outage / 5xx / malformed payload degrades to ``infra_ok=False`` (retryable)
        rather than a false ``ok=False``; a genuine type error is ``ok=False,
        infra_ok=True`` with parsed ``errors``.

    Raises:
        None. Network/HTTP/parse failures are caught and returned as ``infra_ok=False``.
    """
    selected_profile = profile or settings.lean_profile or "core"
    url = f"{settings.lean_url}/verify"
    try:
        resp = httpx.post(
            url,
            json={"source": source, "mode": mode, "profile": selected_profile},
            timeout=timeout or _TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        # Fail-soft: the verifier being down is a retryable infra condition, NOT a
        # proof failure. Surface it distinctly so a caller never reads it as ok=False.
        logger.warning("Lean verifier unreachable (%s) — degrading (infra_ok=False)", exc)
        return _infra_unavailable(mode, exc)

    # A success status with a malformed body is also an infra problem, not a verdict.
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        logger.warning("Lean verifier returned a malformed payload: %r", data)
        return _infra_unavailable(mode, "malformed verifier response")

    raw_errors = data.get("errors") or []
    if not isinstance(raw_errors, list):
        raw_errors = [raw_errors]
    return {
        "ok": bool(data["ok"]),
        "errors": [str(e) for e in raw_errors],
        "elaborated_term": data.get("elaborated_term"),
        "mode": mode,
        "infra_ok": True,
    }


class AxiomsResult(TypedDict):
    """The ``#print axioms`` verdict for one declaration — the soundness signal.

    Attributes:
        ok: True iff the source elaborated and a ``#print axioms`` verdict was found
            for ``decl``. Meaningful only when ``infra_ok`` is True. When False the
            axiom set is meaningless (e.g. the proof itself failed to elaborate, so the
            decl does not exist) — inspect ``errors``.
        axioms: The parsed dependency list (``[]`` for "does not depend on any axioms").
            ``sorryAx`` appears here exactly when a hole was left unclosed — the same
            list that ``crews/soundness`` checks against the standard sound base.
        errors: Parsed compiler diagnostics, or the infra reason when unreachable.
        infra_ok: False ⇒ the verifier service was unreachable/errored (retryable);
            the verdict is meaningless. True ⇒ ``ok``/``axioms`` are a real verdict.
    """

    ok: bool
    axioms: list[str]
    errors: list[str]
    infra_ok: bool


def lean_axioms(
    source: str,
    decl: str,
    *,
    profile: LeanProfile | None = None,
    timeout: float | None = None,
) -> AxiomsResult:
    """Return the ``#print axioms`` dependency set for ``decl`` in ``source``.

    The kernel-grounded soundness chokepoint: the sidecar appends
    ``#print axioms <decl>`` and reports the axioms ``decl`` transitively depends on.
    Same fail-soft contract as :func:`verify_lean` — a sidecar outage degrades to
    ``infra_ok=False`` (retryable), never a false ``ok=False``.

    Args:
        source: The full Lean 4 source containing ``decl`` (typically an already-
            verified proof).
        decl: The declaration name to print axioms for.
        timeout: Optional per-call HTTP timeout (defaults to ``_TIMEOUT_SECONDS``).

    Returns:
        An :class:`AxiomsResult`. Interpretation of the axiom set against the sound
        base lives in :mod:`hyperion.crews.soundness`, not here.
    """
    selected_profile = profile or settings.lean_profile or "core"
    url = f"{settings.lean_url}/axioms"
    try:
        resp = httpx.post(
            url,
            json={"source": source, "decl": decl, "profile": selected_profile},
            timeout=timeout or _TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Lean verifier unreachable (%s) — degrading (infra_ok=False)", exc)
        return {"ok": False, "axioms": [], "errors": [f"lean verifier unavailable: {exc}"], "infra_ok": False}

    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        logger.warning("Lean verifier returned a malformed axioms payload: %r", data)
        return {"ok": False, "axioms": [], "errors": ["malformed verifier response"], "infra_ok": False}

    raw_axioms = data.get("axioms") or []
    if not isinstance(raw_axioms, list):
        raw_axioms = [raw_axioms]
    raw_errors = data.get("errors") or []
    if not isinstance(raw_errors, list):
        raw_errors = [raw_errors]
    return {
        "ok": bool(data["ok"]),
        "axioms": [str(a) for a in raw_axioms],
        "errors": [str(e) for e in raw_errors],
        "infra_ok": True,
    }


class LeanVerifyTool(BaseTool):
    """CrewAI tool wrapper around :func:`verify_lean` for agent use.

    Granted to agents (e.g. a repair agent that owns its own inner loop) so they can
    type-check a candidate as a tool call. Native nodes do NOT use this wrapper — they
    call :func:`verify_lean` directly (no ReAct loop), per build-plan §1.

    The verdict is returned as a compact string the LLM can read. The unfakeable
    invariant holds either way: ``ok`` always comes from the kernel.
    """

    name: str = "lean_verify"
    description: str = (
        "Type-check a Lean 4 source string against the Lean verifier. "
        "Input: 'source' (the full Lean 4 source) and optional 'mode' "
        "('full' = no sorry allowed, the proof must close; 'skeleton' = sorry allowed). "
        "Returns OK with the elaborated term, FAILED with compiler errors, or "
        "VERIFIER_UNAVAILABLE (a retryable infra condition, not a proof failure)."
    )

    def _run(self, source: str, mode: str = "full") -> str:
        """Verify ``source`` and return a compact human-/LLM-readable verdict string."""
        safe_mode: VerifyMode = mode if mode in ("skeleton", "full") else "full"
        res = verify_lean(source, mode=safe_mode)
        if not res["infra_ok"]:
            return "VERIFIER_UNAVAILABLE: " + "; ".join(res["errors"])
        if res["ok"]:
            term = res["elaborated_term"]
            return "OK" + (f"\nelaborated_term: {term}" if term else "")
        return "FAILED:\n" + "\n".join(res["errors"])
