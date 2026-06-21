"""The soundness contract — the ``sorryAx`` gate, operationalized.

Mirrors Aristotle's standard: a problem is **solved** only if it produces a complete
Lean 4 + Mathlib proof **without gaps or unsound axioms like ``sorryAx``**. We enforce
that by reading the declaration's ``#print axioms`` dependency set (via the Lean sidecar,
:func:`hyperion.tools.lean_verify.lean_axioms`) and checking it is within the standard
sound base.

Why this is *also* the completeness check
-----------------------------------------
In the draft-sketch-prove flow an unfilled hole is a ``sorry``, and ``sorry`` elaborates
to ``sorryAx`` — so ``#print axioms`` reporting ``sorryAx`` *is* the signal that a hole
was never closed. The one gate that rejects unsound axioms therefore also rejects
incomplete proofs: **"soundness-clean" ≡ "every sketch hole closed."**

Two layers, so the logic is unit-testable offline without a live Lean:
  - :func:`axioms_clean` / :func:`source_declares_gap` — pure predicates over an axiom
    set / a source string.
  - :func:`soundness_ok` — calls the sidecar then applies those predicates, returning a
    structured :class:`SoundnessResult`. Call it at *every* acceptance point (each bridge
    lemma, each planned lemma, the parent theorem) before a proof counts as solved or is
    banked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from hyperion.tools.lean_verify import lean_axioms

# Lean's standard sound axiom base — the three classical foundations Mathlib rests on.
# A proof depending only on these (or on nothing) is kernel-sound.
SOUND_BASE: frozenset[str] = frozenset({"propext", "Classical.choice", "Quot.sound"})

# native_decide's trust rests on the compiler, not the kernel. Permitted in lax mode,
# rejected in strict (headline-run) mode. ``Lean.ofReduceBool``/``ofReduceNat`` are the
# axioms ``native_decide``/``decide`` emit; ``trustCompiler`` covers the general case.
NATIVE_AXIOMS: frozenset[str] = frozenset(
    {"Lean.ofReduceBool", "Lean.ofReduceNat", "Lean.trustCompiler"}
)

# Forbidden gap tokens in a source: ``sorry``/``admit`` as standalone identifiers (not as
# a substring of a larger name). ``#print axioms`` already catches ``sorryAx`` robustly;
# this is a cheap belt-and-suspenders gate for sources before/without an axioms probe.
_GAP_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.])(sorry|admit)(?![A-Za-z0-9_])")
# A new ``axiom`` declaration (user-introduced trust) at the start of a line.
_AXIOM_DECL_RE = re.compile(r"(?m)^\s*axiom\s")


def allowed_axioms(*, strict: bool) -> frozenset[str]:
    """The axiom set a soundness-clean proof may depend on, given the trust mode.

    Strict (recommended for headline runs) is the kernel-only :data:`SOUND_BASE`; lax
    additionally tolerates the compiler-trusting :data:`NATIVE_AXIOMS` (``native_decide``).
    ``sorryAx`` and user-declared axioms are in neither set, so they are always rejected.
    """
    return SOUND_BASE if strict else (SOUND_BASE | NATIVE_AXIOMS)


def axioms_clean(axioms: Iterable[str], *, strict: bool = False) -> bool:
    """True iff every axiom in ``axioms`` is within :func:`allowed_axioms`.

    This single subset check enforces the whole contract: it rejects ``sorryAx`` (gap),
    any user-declared axiom (unsound trust), and — in strict mode — ``native_decide``.
    """
    return set(axioms) <= allowed_axioms(strict=strict)


def source_declares_gap(source: str) -> bool:
    """True iff ``source`` textually contains a ``sorry``/``admit`` or a new ``axiom``.

    A cheap pre-check; the authoritative signal is the ``#print axioms`` set, but this
    catches gaps before paying for an elaboration and guards sources that are never
    individually axiom-probed.
    """
    return bool(_GAP_TOKEN_RE.search(source) or _AXIOM_DECL_RE.search(source))


@dataclass
class SoundnessResult:
    """Verdict of the soundness contract for one declaration.

    Attributes:
        ok: True iff the proof is soundness-clean: it elaborated, a ``#print axioms``
            verdict was obtained, the source declares no gap, and the axiom set is within
            the allowed base. This is the value acceptance/banking gates on.
        infra_ok: False ⇒ the verifier was unreachable (retryable); ``ok`` is meaningless.
        axioms: The parsed dependency set (empty when the proof depends on no axioms).
        reasons: Human-readable reasons ``ok`` is False (empty when ``ok``).
    """

    ok: bool
    infra_ok: bool
    axioms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def soundness_ok(
    source: str,
    decl: str,
    *,
    strict: bool = False,
    profile: str | None = None,
    timeout: float | None = None,
) -> SoundnessResult:
    """Enforce the soundness contract on ``decl`` within ``source``.

    Runs the cheap source-gap gate, then the kernel-grounded ``#print axioms`` check via
    the sidecar. Returns a structured :class:`SoundnessResult`; infra outages surface as
    ``infra_ok=False`` (retryable), never a false ``ok=False`` — same contract as the rest
    of the verifier client.

    Args:
        source: The full Lean 4 source (typically an already type-checked proof).
        decl: The declaration whose axiom dependencies are checked.
        strict: If True, reject ``native_decide``/``Lean.ofReduceBool`` (headline runs).
        timeout: Optional per-call HTTP timeout forwarded to the sidecar.
    """
    reasons: list[str] = []
    if source_declares_gap(source):
        reasons.append("source contains sorry/admit or a user-declared axiom")

    res = lean_axioms(source, decl, profile=profile, timeout=timeout)
    if not res["infra_ok"]:
        return SoundnessResult(ok=False, infra_ok=False, reasons=res["errors"])

    if not res["ok"]:
        # The decl did not elaborate / no axioms verdict — not soundness-clean.
        reasons.extend(res["errors"] or [f"no axioms verdict for '{decl}'"])
        return SoundnessResult(ok=False, infra_ok=True, axioms=res["axioms"], reasons=reasons)

    axioms = res["axioms"]
    if not axioms_clean(axioms, strict=strict):
        offending = sorted(set(axioms) - allowed_axioms(strict=strict))
        reasons.append("disallowed axioms: " + ", ".join(offending))

    return SoundnessResult(ok=not reasons, infra_ok=True, axioms=axioms, reasons=reasons)
