"""Tests for the lemma bank (build-plan Phase 2).

Unit tier, fully offline: Qdrant and the embedding client are mocked via
``lemma_bank._get_clients``; no live Qdrant or LiteLLM proxy is touched.

The four DoD assertions (build plan Phase 2 Test Gate):
  - Storing the same lemma twice yields ONE Qdrant point (deterministic UUID5).
  - ``retrieve_lemmas`` returns payloads ranked by vector score.
  - A simulated write failure is observable — logged at ERROR and returned as
    ``ok=False`` — not silently swallowed (the load-bearing risk #4 decision).
  - No live Qdrant required.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from hyperion.memory import lemma_bank


def _mock_clients(qdrant: MagicMock | None = None):
    """Build (oai, qdrant) mocks; oai returns a fixed embedding vector."""
    oai = MagicMock()
    oai.embeddings.create.return_value = MagicMock(data=[MagicMock(embedding=[0.1, 0.2, 0.3])])
    qdrant = qdrant or MagicMock()
    return oai, qdrant


def _hit(score: float, **payload):
    """A fake Qdrant ScoredPoint with ``.score`` and ``.payload``."""
    h = MagicMock()
    h.score = score
    h.payload = payload
    return h


# ---------------------------------------------------------------------------
# Dedup — same lemma stored twice → one point (deterministic UUID5)
# ---------------------------------------------------------------------------


def test_same_lemma_twice_is_one_point():
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        r1 = lemma_bank.store_lemma("theorem add_comm : a + b = b + a", "by ring")
        r2 = lemma_bank.store_lemma("theorem add_comm : a + b = b + a", "by ring")

    assert r1["ok"] and r2["ok"]
    # Both upserts target the SAME deterministic point id → an upsert, not a duplicate.
    assert r1["id"] == r2["id"]
    ids = [call.kwargs["points"][0].id for call in qdrant.upsert.call_args_list]
    assert ids[0] == ids[1]


def test_dedup_id_is_whitespace_normalized():
    """Whitespace-only differences hash to the same point id (scoped normalization)."""
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        a = lemma_bank.store_lemma("theorem t :  a = a", "rfl")
        b = lemma_bank.store_lemma("theorem t : a = a", "rfl")
        c = lemma_bank.store_lemma("theorem  t   :\n  a = a", "rfl")
    assert a["id"] == b["id"] == c["id"]


def test_distinct_statements_get_distinct_ids():
    assert lemma_bank._point_id("theorem t : a = a") != lemma_bank._point_id("theorem t : b = b")


# ---------------------------------------------------------------------------
# Store — payload schema + statement is the embedded text
# ---------------------------------------------------------------------------


def test_store_writes_full_payload_and_embeds_statement():
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        res = lemma_bank.store_lemma(
            "theorem add_zero : n + 0 = n",
            "by simp",
            generality_score=0.8,
            source_goal="prove n + 0 = n",
            verification_mode="full",
            verified_at=12345,
        )

    assert res["ok"] is True
    # The embedded text is the statement, not request+summary.
    oai.embeddings.create.assert_called_once()
    assert oai.embeddings.create.call_args.kwargs["input"] == "theorem add_zero : n + 0 = n"

    payload = qdrant.upsert.call_args.kwargs["points"][0].payload
    assert payload["statement"] == "theorem add_zero : n + 0 = n"
    assert payload["lean_type"] == "theorem add_zero : n + 0 = n"
    assert payload["proof_term"] == "by simp"
    assert payload["origin"] == "skill_library"
    assert payload["source_collection"] == lemma_bank._skill_collection()
    assert payload["generality_score"] == 0.8
    assert payload["source_goal"] == "prove n + 0 = n"
    assert payload["provenance"] == {"source_goal": "prove n + 0 = n"}
    assert payload["verified_at"] == 12345
    assert payload["verification_mode"] == "full"
    assert payload["normalized_key"] == "theorem add_zero : n + 0 = n"
    assert payload["times_retrieved"] == 0
    assert payload["times_won"] == 0
    assert {"add_zero", "+", "="}.issubset(set(payload["symbol_set"]))


def test_store_embeds_lean_type_when_available():
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        lemma_bank.store_lemma(
            "theorem add_zero (n : Nat) : n + 0 = n := by simp",
            "by simp",
            lean_type="∀ (n : Nat), n + 0 = n",
        )

    assert oai.embeddings.create.call_args.kwargs["input"] == "∀ (n : Nat), n + 0 = n"
    payload = qdrant.upsert.call_args.kwargs["points"][0].payload
    assert payload["lean_type"] == "∀ (n : Nat), n + 0 = n"
    assert "n" not in payload["symbol_set"]  # local binder, not a reusable symbol


def test_store_defaults_verified_at_to_now():
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        with patch.object(lemma_bank.time, "time", return_value=999.0):
            lemma_bank.store_lemma("theorem t : True", "trivial")
    payload = qdrant.upsert.call_args.kwargs["points"][0].payload
    assert payload["verified_at"] == 999


# ---------------------------------------------------------------------------
# Retrieve — ranked by vector score; fail-soft
# ---------------------------------------------------------------------------


def test_retrieve_returns_payloads_ranked_by_score():
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    # Qdrant returns hits already in descending-score order; we honor that order.
    qdrant.query_points.return_value = MagicMock(
        points=[
            _hit(0.91, statement="lemma A", proof_term="pa", generality_score=0.5,
                 source_goal="g", verified_at=1, verification_mode="full"),
            _hit(0.42, statement="lemma B", proof_term="pb", generality_score=0.1,
                 source_goal="g", verified_at=2, verification_mode="full"),
        ]
    )
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        out = lemma_bank.retrieve_lemmas("some goal", limit=5)

    assert [r["statement"] for r in out] == ["lemma A", "lemma B"]
    assert [r["score"] for r in out] == [0.91, 0.42]  # ranked, descending
    assert out[0]["proof_term"] == "pa"
    assert out[0]["origin"] == "skill_library"
    assert out[0]["source_collection"] == lemma_bank._skill_collection()
    assert out[0]["times_retrieved"] == 0
    # The goal is the embedded query text.
    assert oai.embeddings.create.call_args.kwargs["input"] == "some goal"


def test_retrieve_failure_is_fail_soft_returns_empty(caplog):
    qdrant = MagicMock()
    qdrant.query_points.side_effect = Exception("qdrant unreachable")
    oai, _ = _mock_clients(qdrant)
    with caplog.at_level(logging.WARNING, logger=lemma_bank.logger.name):
        with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
            out = lemma_bank.retrieve_lemmas("goal")
    assert out == []  # degraded read never breaks a run
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_bump_times_won_updates_payload_best_effort():
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        count = lemma_bank.bump_times_won({"id": "pt1", "times_won": 2})

    assert count == 3
    qdrant.set_payload.assert_called_once_with(
        collection_name=lemma_bank._collection(),
        payload={"times_won": 3},
        points=["pt1"],
    )


def test_bump_times_won_without_point_id_still_returns_incremented_count():
    with patch.object(lemma_bank, "_get_clients") as clients:
        count = lemma_bank.bump_times_won({"times_won": 4})

    assert count == 5
    clients.assert_not_called()


def test_bump_times_won_skips_mathlib_premises():
    with patch.object(lemma_bank, "_get_clients") as clients:
        count = lemma_bank.bump_times_won({
            "id": "mathlib-pt",
            "times_won": 0,
            "source_collection": lemma_bank._mathlib_collection(),
        })

    assert count == 1
    clients.assert_not_called()


def test_retrieve_mathlib_premises_has_static_source_fields():
    qdrant = MagicMock()
    oai, _ = _mock_clients(qdrant)
    qdrant.query_points.return_value = MagicMock(
        points=[
            _hit(0.88, name="Nat.zero_add", signature="∀ (n : Nat), 0 + n = n",
                 premises_used=["Nat.add_zero"], source="mathlib"),
        ]
    )
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        out = lemma_bank.retrieve_mathlib_premises("0 + 0 = 0", limit=3)

    assert out[0]["origin"] == "mathlib"
    assert out[0]["source_collection"] == lemma_bank._mathlib_collection()
    assert out[0]["name"] == "Nat.zero_add"
    assert out[0]["premises_used"] == ["Nat.add_zero"]
    qdrant.set_payload.assert_not_called()


# ---------------------------------------------------------------------------
# Write-path loudness — the load-bearing risk #4 decision, proven by test
# ---------------------------------------------------------------------------


def test_write_failure_is_loud_and_observable(caplog):
    """A failed bank write must be logged at ERROR and returned as ok=False —
    never silently swallowed (a lost verified lemma stalls the snowball)."""
    qdrant = MagicMock()
    qdrant.upsert.side_effect = Exception("disk full")
    oai, _ = _mock_clients(qdrant)
    with caplog.at_level(logging.ERROR, logger=lemma_bank.logger.name):
        with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
            res = lemma_bank.store_lemma("theorem t : True", "trivial")

    # Observable in the return value...
    assert res["ok"] is False
    assert res["error"] is not None and "disk full" in res["error"]
    assert res["id"] is not None  # id known, so a retry can target the same point
    # ...and loud in the logs (ERROR, not the warning episodic uses).
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


def test_store_does_not_raise_on_failure():
    """Loud, but non-crashing: a banking failure must not kill an otherwise-good run."""
    oai = MagicMock()
    oai.embeddings.create.side_effect = Exception("embedding service down")
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, MagicMock())):
        res = lemma_bank.store_lemma("theorem t : True", "trivial")  # must not raise
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# Self-healing write — the collection is provisioned on first write (was the
# cold-start failure: lemma_bank never existed, so every store 404'd).
# ---------------------------------------------------------------------------


def test_store_creates_missing_collection_with_embedding_dims():
    """A missing collection is created on write, sized to the live embedding (not hardcoded)."""
    qdrant = MagicMock()
    qdrant.collection_exists.return_value = False
    oai, _ = _mock_clients(qdrant)  # embedding is a 3-vector
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        res = lemma_bank.store_lemma("theorem t : a = a", "rfl")

    assert res["ok"]
    qdrant.create_collection.assert_called_once()
    # Dims come from len(vector), so the collection can never drift from the model.
    assert qdrant.create_collection.call_args.kwargs["vectors_config"].size == 3
    # And the upsert still happens after provisioning.
    qdrant.upsert.assert_called_once()


def test_store_skips_create_when_collection_exists():
    """The ensure step is a no-op when the collection is already present (idempotent)."""
    qdrant = MagicMock()
    qdrant.collection_exists.return_value = True
    oai, _ = _mock_clients(qdrant)
    with patch.object(lemma_bank, "_get_clients", return_value=(oai, qdrant)):
        lemma_bank.store_lemma("theorem t : a = a", "rfl")

    qdrant.create_collection.assert_not_called()
    qdrant.upsert.assert_called_once()
