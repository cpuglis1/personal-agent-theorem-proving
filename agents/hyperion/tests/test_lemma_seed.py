"""Tests for the cold-start lemma seed (``hyperion.memory.lemma_seed``).

Unit tier, fully offline: ``lemma_bank.store_lemma`` is mocked so no Qdrant/LiteLLM is
touched. The seeds themselves were confirmed ``ok=True`` against the live kernel; here we
only pin the seeding *loop's* contract (every seed written; failures surfaced, not raised).
"""

from __future__ import annotations

from unittest.mock import patch

from hyperion.memory import lemma_seed


def test_seed_stores_every_starter_lemma():
    """Each seed is written once, with its statement/proof_term/lean_type passed through."""
    calls = []

    def fake_store(statement, proof_term, **kwargs):
        calls.append((statement, proof_term, kwargs.get("lean_type")))
        return {"ok": True, "id": "x", "error": None}

    with patch.object(lemma_seed.lemma_bank, "store_lemma", side_effect=fake_store):
        summary = lemma_seed.seed_lemma_bank()

    assert summary == {"ok": len(lemma_seed.SEED_LEMMAS), "failed": 0, "failures": []}
    assert len(calls) == len(lemma_seed.SEED_LEMMAS)
    # lean_type is forwarded as a first-class field for the applicability probe.
    assert all(lean_type for _, _, lean_type in calls)


def test_seed_surfaces_failures_without_raising():
    """A failed seed write is collected into ``failures`` rather than aborting the rest."""
    def flaky_store(statement, proof_term, **kwargs):
        ok = "nat_refl" in statement  # only the first seed "succeeds"
        return {"ok": ok, "id": "x", "error": None if ok else "qdrant down"}

    with patch.object(lemma_seed.lemma_bank, "store_lemma", side_effect=flaky_store):
        summary = lemma_seed.seed_lemma_bank()  # must not raise

    assert summary["ok"] == 1
    assert summary["failed"] == len(lemma_seed.SEED_LEMMAS) - 1
    assert all(f["error"] == "qdrant down" for f in summary["failures"])
