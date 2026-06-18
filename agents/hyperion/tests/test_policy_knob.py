"""RESEARCH/DEPLOY policy knob — node firing flips with ``settings.prover_research_mode``.

Build-plan Post-work #2 DoD: "Flipping the RESEARCH/DEPLOY flag measurably changes whether
``synthesize`` fires (asserted by a routing test, like the ``skipped`` records in
``_node_fires``)." The gate is the ``NodeWhen.prover_mode`` field consulted by
``runner._node_fires`` — pure routing, no run needed.
"""

from __future__ import annotations

from unittest.mock import patch

from hyperion.config import settings
from hyperion.crews import runner
from hyperion.crews.workflows import NodeWhen, WorkflowNode


def _synth(prover_mode):
    return WorkflowNode(id="synthesize", kind="work", agent="lemma_synthesizer",
                        when=NodeWhen(prover_mode=prover_mode), upstream=["skeleton_check"])


def test_research_gated_node_fires_only_in_research_mode():
    node = _synth("research")
    with patch.object(settings, "prover_research_mode", True):
        fires, _ = runner._node_fires(node, None)
        assert fires is True
    with patch.object(settings, "prover_research_mode", False):
        fires, reason = runner._node_fires(node, None)
        assert fires is False
        assert "DEPLOY" in reason


def test_deploy_gated_node_fires_only_in_deploy_mode():
    node = _synth("deploy")
    with patch.object(settings, "prover_research_mode", False):
        assert runner._node_fires(node, None)[0] is True
    with patch.object(settings, "prover_research_mode", True):
        fires, reason = runner._node_fires(node, None)
        assert fires is False
        assert "RESEARCH" in reason


def test_mode_agnostic_node_always_fires():
    # No prover_mode and no task_types ⇒ always fires (the historical default, unchanged).
    node = WorkflowNode(id="verify", kind="native", handler="verify", upstream=[])
    with patch.object(settings, "prover_research_mode", True):
        assert runner._node_fires(node, None)[0] is True
    with patch.object(settings, "prover_research_mode", False):
        assert runner._node_fires(node, None)[0] is True


def test_prover_mode_and_task_type_both_apply():
    # When both are set, both must hold. Here the mode passes but the task_type does not.
    node = WorkflowNode(id="synthesize", kind="work", agent="lemma_synthesizer",
                        when=NodeWhen(prover_mode="research", task_types=["code"]),
                        upstream=[])

    class _Sig:
        task_type = "research"  # not "code"

    with patch.object(settings, "prover_research_mode", True):
        fires, reason = runner._node_fires(node, _Sig())
        assert fires is False
        assert "task_type" in reason
