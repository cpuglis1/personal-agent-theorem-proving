"""Cold-start seed lemmas for the prover's :mod:`hyperion.memory.lemma_bank`.

Role in the system
------------------
A fresh ``lemma_bank`` is empty, so Path A (retrieve) has nothing to return until the
prover has banked something itself — the snowball cannot start from a cold stone. This
module provides a small starter library (à la Seed-Prover's lemma library) so retrieval
returns *applicable* lemmas on the very first run, before the system has proved anything.

Scope of the starter set (deliberately core-Lean)
-------------------------------------------------
Every seed is a **closed, self-contained proposition over ``Nat``** whose proof term
type-checks WITHOUT ``import Mathlib`` — i.e. they verify under today's perf envelope
(the warm-Mathlib REPL is build-plan priority 3, not yet in). Each was confirmed
``ok=True`` against the live kernel in ``full`` mode. They are written so the Path-A
candidate assembler (``_candidate_from_lemma``: ``example : <goal> := <proof_term>``)
produces a verifiable source when a later goal matches the seed's ``lean_type``.

Each entry carries the three fields the bank + applicability gate need:
  - ``statement``  — a named ``theorem`` (embedded for retrieval; ``_lemma_type`` reads it),
  - ``proof_term`` — the verified term that closes it (reused verbatim by Path A),
  - ``lean_type``  — the bare proposition (the first-class field the Phase-3 probe uses).
"""

from __future__ import annotations

import logging
from typing import Any

from hyperion.memory import lemma_bank

logger = logging.getLogger(__name__)

# (statement, proof_term, lean_type) — all verified ok=True in full mode, core Lean only.
SEED_LEMMAS: list[dict[str, str]] = [
    {
        "statement": "theorem nat_refl : ∀ (n : Nat), n = n := fun _ => rfl",
        "proof_term": "fun _ => rfl",
        "lean_type": "∀ (n : Nat), n = n",
    },
    {
        "statement": "theorem nat_add_zero : ∀ (n : Nat), n + 0 = n := fun n => Nat.add_zero n",
        "proof_term": "fun n => Nat.add_zero n",
        "lean_type": "∀ (n : Nat), n + 0 = n",
    },
    {
        "statement": "theorem nat_zero_add : ∀ (n : Nat), 0 + n = n := fun n => Nat.zero_add n",
        "proof_term": "fun n => Nat.zero_add n",
        "lean_type": "∀ (n : Nat), 0 + n = n",
    },
    {
        "statement": (
            "theorem nat_add_zero_comm : ∀ (n : Nat), n + 0 = 0 + n := "
            "fun n => by rw [Nat.add_zero, Nat.zero_add]"
        ),
        "proof_term": "fun n => by rw [Nat.add_zero, Nat.zero_add]",
        "lean_type": "∀ (n : Nat), n + 0 = 0 + n",
    },
    {
        "statement": "theorem nat_add_comm : ∀ (a b : Nat), a + b = b + a := Nat.add_comm",
        "proof_term": "Nat.add_comm",
        "lean_type": "∀ (a b : Nat), a + b = b + a",
    },
    {
        "statement": "theorem nat_mul_one : ∀ (n : Nat), n * 1 = n := fun n => Nat.mul_one n",
        "proof_term": "fun n => Nat.mul_one n",
        "lean_type": "∀ (n : Nat), n * 1 = n",
    },
]


def seed_lemma_bank() -> dict[str, Any]:
    """Idempotently store the starter lemmas in the bank, returning a write summary.

    Each lemma is upserted via :func:`lemma_bank.store_lemma`, which self-provisions the
    collection on first write and dedups on the normalized statement — so re-running is
    safe (the same seeds upsert in place rather than duplicating). Mirrors the bank's
    loud-write posture: a failed seed is collected into ``failures`` rather than raised,
    so one bad seed never aborts the rest.

    Returns:
        ``{"ok": int, "failed": int, "failures": [{"statement", "error"}, ...]}``.
    """
    ok = 0
    failures: list[dict[str, str]] = []
    for seed in SEED_LEMMAS:
        res = lemma_bank.store_lemma(
            seed["statement"],
            seed["proof_term"],
            source_goal="(seed)",
            verification_mode="full",
            generality_score=1.0,
            lean_type=seed["lean_type"],
        )
        if res["ok"]:
            ok += 1
        else:
            failures.append({"statement": seed["statement"], "error": res["error"] or "unknown"})
    if failures:
        logger.error("seed_lemma_bank: %d/%d seed write(s) failed: %s",
                     len(failures), len(SEED_LEMMAS), failures)
    else:
        logger.info("seed_lemma_bank: stored %d starter lemma(s)", ok)
    return {"ok": ok, "failed": len(failures), "failures": failures}
