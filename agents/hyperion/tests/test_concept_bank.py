from __future__ import annotations

from unittest.mock import MagicMock, patch

from hyperion.config import settings
from hyperion.memory import concept_bank


def _concept(**overrides):
    base = {
        "concept_id": "c1",
        "definition": {"name": "Balanced", "source": "def Balanced : Prop := True"},
        "bridges": [{"name": "Balanced.intro", "lean_type": "Balanced", "source": "theorem x : Balanced := trivial"}],
        "origin": "synthesized",
        "times_won": 1,
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
