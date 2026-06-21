from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hyperion.config import settings
from hyperion.memory import concept_bank


def _concept(**overrides):
    base = {
        "concept_id": "c1",
        "definition": {"name": "Balanced", "source": "def Balanced : Prop := True"},
        "bridges": [{"name": "Balanced.intro", "lean_type": "Balanced", "source": "theorem x : Balanced := trivial"}],
        "origin": "synthesized",
        "times_won": 1,
        "necessity_hits": 0,
        "provisional": True,
        "birth_ablation": {"budget": 3},
    }
    base.update(overrides)
    return base


def test_store_concept_is_loud_and_payloaded(monkeypatch):
    qdrant = MagicMock()
    with patch.object(concept_bank, "_get_clients", return_value=(MagicMock(), qdrant)), \
         patch.object(concept_bank, "_embed", return_value=[0.1, 0.2]), \
         patch.object(concept_bank, "_ensure_collection") as ensure:
        res = concept_bank.store_concept(_concept(), source_goal="prove G", theorem_id="t1", verified_at=123)

    assert res["ok"] is True
    ensure.assert_called_once()
    kwargs = qdrant.upsert.call_args.kwargs
    assert kwargs["collection_name"] == settings.qdrant_concepts_collection
    point = kwargs["points"][0]
    assert point.payload["concept_id"] == "c1"
    assert point.payload["definition"]["name"] == "Balanced"
    assert point.payload["source_goal"] == "prove G"
    assert point.payload["theorem_id"] == "t1"
    assert point.payload["provisional"] is True


def test_store_concept_surfaces_write_failure():
    qdrant = MagicMock()
    qdrant.upsert.side_effect = RuntimeError("qdrant down")
    with patch.object(concept_bank, "_get_clients", return_value=(MagicMock(), qdrant)), \
         patch.object(concept_bank, "_embed", return_value=[0.1]), \
         patch.object(concept_bank, "_ensure_collection"):
        res = concept_bank.store_concept(_concept())

    assert res["ok"] is False
    assert "qdrant down" in (res["error"] or "")


def test_retrieve_concepts_fail_soft():
    with patch.object(concept_bank, "_get_clients", side_effect=RuntimeError("offline")):
        assert concept_bank.retrieve_concepts("G") == []


def test_mark_necessity_hit_promotes_at_threshold(monkeypatch):
    monkeypatch.setattr(settings, "concept_promote_k", 2)
    qdrant = MagicMock()
    concept = _concept(id="pt", necessity_hits=1, provisional=True)
    with patch.object(concept_bank, "_get_clients", return_value=(MagicMock(), qdrant)):
        updated = concept_bank.mark_necessity_hit(concept, theorem_id="later", theorem_index=7)

    assert updated["necessity_hits"] == 2
    assert updated["provisional"] is False
    payload = qdrant.set_payload.call_args.kwargs["payload"]
    assert payload["necessity_hits"] == 2
    assert payload["provisional"] is False
    assert payload["last_used_theorem_index"] == 7


def test_prune_idle_concepts_deletes_only_expired_provisional(monkeypatch):
    monkeypatch.setattr(settings, "concept_prune_idle_m", 3)
    qdrant = MagicMock()
    concepts = [
        _concept(id="old", born_theorem_index=1, provisional=True),
        _concept(id="fresh", born_theorem_index=4, provisional=True),
        _concept(id="durable", born_theorem_index=1, provisional=False),
    ]
    with patch.object(concept_bank, "_get_clients", return_value=(MagicMock(), qdrant)):
        pruned = concept_bank.prune_idle_concepts(concepts, current_theorem_index=5)

    assert [c["id"] for c in pruned] == ["old"]
    qdrant.delete.assert_called_once()
