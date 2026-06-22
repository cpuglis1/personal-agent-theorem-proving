# Lean Prove Pipeline

This document describes the current trimmed `lean-prove` runtime. Older compare,
abstract, birth-ablation, weak/strong-regime, and cross-theorem promotion machinery has
been removed.

## DAG

```text
formal_ingest
  -> decompose
  -> skeleton_check
  -> retrieve || synthesize
  -> verify
  -> bank

verify stall
  -> escalation_gate
  -> synthesize_definition
  -> verify_concept
  -> prove_through
  -> bank_concept
  -> bank
```

For multi-`sorry` scaffolds, the runner fans out the per-subgoal segment from
`retrieve || synthesize` through the concept branch and fans back into `bank`.

## Stage Contracts

- `formal_ingest`: deterministic parsing/normalization of formal input when present.
- `decompose`: LLM proposes a Lean `have`-chain scaffold and typed subgoals.
- `skeleton_check`: Lean checks the scaffold shape with `sorry` allowed.
- `retrieve`: optional cache lookup for already banked lemmas/concepts.
- `synthesize`: LLM proposes a self-contained Lean proof candidate for one subgoal.
- `verify`: kernel-owned controller. It tries Path A candidates first, then the
  deterministic closer battery, then the synthesized seed plus bounded repair. It writes
  `discharged:<sg>` only when Lean full mode accepts a proof with no `sorry`.
- `escalation_gate`: routes only genuine normal-path stalls to definition synthesis.
- `synthesize_definition`: LLM proposes a definition plus bridge lemmas; cheap degeneracy
  filters reject obvious non-concepts before proof budget is spent.
- `verify_concept`: the definition must elaborate and every bridge must be kernel-proved
  and soundness-clean.
- `prove_through`: reattempts the stalled goal with the verified definition and bridges in
  scope. If this closes soundness-clean, it stages `accepted_concept:<sg>` and
  `discharged:<sg>` with `path: "C"`.
- `bank_concept`: persists accepted concepts when learning writes are enabled.
- `bank`: assembles `artifacts/result.lean`, runs final full-mode Lean verification, and
  persists winning lemmas.

## What Is Gone

- `compare`, `lemma_compare`, and `triple_log`.
- `abstract`, `abstractor`, `propose_abstraction`, and `abstracted`.
- `birth_ablation` and per-goal WITH/WITHOUT causal acceptance.
- Weak/strong prover regimes, banned-tactic eligibility, and `b_strong` counters.
- Cross-theorem concept promotion/pruning fields such as `necessity_hits`.

## Measurement

The thesis measurement is before/after, not per-goal causal testing:

- **Escalation OFF**: run the basic pipeline only.
- **Escalation ON**: run the same budget with definition synthesis enabled.

The result is the set of goals that close only with escalation ON, where the closing proof
goes through a synthesized definition whose bridges are kernel-verified and soundness-clean.
Keep proving budgets equal across OFF and ON runs.

## Trace Surface

Keep these trace fields:

- plan/scaffold and skeleton verdict
- per-subgoal candidates and verification decision
- tier/winner via `discharged:<sg>.origin` and `discharged:<sg>.path`
- `escalated`
- definition synthesis candidates
- `verified_concept`, `prove_through`, `accepted_concept`, `bank_concept`
- `final_verify`

Do not reintroduce `triple_log`, `abstracted`, `birth_ablation_pass`,
`b_strong_closed`, or `necessity_hits`.
