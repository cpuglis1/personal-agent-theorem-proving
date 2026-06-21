"""Review-first theorem stream for the definition-synthesis empirical run.

This module defines a small related stream of core-Lean relation-composition goals.
It does not run by default: ``python -m hyperion.eval.concept_stream`` prints the plan
for manual review. Add ``--run`` later to submit the stream to the real ``lean-prove``
workflow after the bank reset and prompt/model settings are confirmed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any

from hyperion.config import settings
from hyperion.crews.runner import run_task


@dataclass(frozen=True)
class StreamTheorem:
    id: str
    goal: str
    role: str
    expected_concept: str
    review_note: str


RELATION_COMPOSITION_STREAM: list[StreamTheorem] = [
    StreamTheorem(
        id="relcomp_birth_assoc_left",
        goal=(
            "forall {alpha : Type} (R S T : alpha -> alpha -> Prop) (a d : alpha), "
            "(Exists fun b => And (R a b) (Exists fun c => And (S b c) (T c d))) -> "
            "Exists fun c => And (Exists fun b => And (R a b) (S b c)) (T c d)"
        ),
        role="birth",
        expected_concept="RelComp R S a c := Exists fun b => And (R a b) (S b c)",
        review_note=(
            "Expanded associativity direction. If normal proving stalls, a useful concept is "
            "binary relation composition."
        ),
    ),
    StreamTheorem(
        id="relcomp_reuse_assoc_right",
        goal=(
            "forall {alpha : Type} (R S T : alpha -> alpha -> Prop) (a d : alpha), "
            "(Exists fun c => And (Exists fun b => And (R a b) (S b c)) (T c d)) -> "
            "Exists fun b => And (R a b) (Exists fun c => And (S b c) (T c d))"
        ),
        role="reuse",
        expected_concept="RelComp",
        review_note="Converse associativity direction; should be easier with the same concept.",
    ),
    StreamTheorem(
        id="relcomp_reuse_identity_left",
        goal=(
            "forall {alpha : Type} (R : alpha -> alpha -> Prop) (a c : alpha), "
            "R a c -> Exists fun b => And (Eq a b) (R b c)"
        ),
        role="reuse",
        expected_concept="RelComp with the identity relation",
        review_note="Identity-left shape for relation composition.",
    ),
    StreamTheorem(
        id="relcomp_reuse_identity_right",
        goal=(
            "forall {alpha : Type} (R : alpha -> alpha -> Prop) (a c : alpha), "
            "R a c -> Exists fun b => And (R a b) (Eq b c)"
        ),
        role="reuse",
        expected_concept="RelComp with the identity relation",
        review_note="Identity-right shape for relation composition.",
    ),
]


def stream_manifest() -> dict[str, Any]:
    return {
        "name": "relation-composition-concept-stream",
        "workflow": "lean-prove",
        "strict_soundness": True,
        "success_criteria": [
            "at least one accepted_concept whose definition is synthesized, not seeded",
            "every bridge has axioms_clean=true and no sorryAx",
            f"necessity_hits >= {settings.concept_promote_k} on a later theorem",
        ],
        "theorems": [asdict(t) for t in RELATION_COMPOSITION_STREAM],
    }


async def run_stream() -> list[dict[str, Any]]:
    settings.prover_soundness_strict = True
    results: list[dict[str, Any]] = []
    for theorem in RELATION_COMPOSITION_STREAM:
        result = await run_task(theorem.id, theorem.goal, workflow="lean-prove")
        results.append({"id": theorem.id, "result": result})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="submit the stream to lean-prove")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(stream_manifest(), indent=2))
        return
    print(json.dumps(asyncio.run(run_stream()), indent=2, default=str))


if __name__ == "__main__":
    main()
