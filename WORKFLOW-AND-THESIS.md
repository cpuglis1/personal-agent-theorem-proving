# Workflow And Thesis

This is the current thesis framing for `lean-prove` after the DAG trim.

## Claim

When the basic verifier-in-the-loop pipeline fails to close a goal, does
LLM-proposed definition synthesis, checked by the Lean kernel, let it close goals it
otherwise could not?

The answer is measured with an equal-budget OFF/ON eval:

- **OFF**: basic pipeline only.
- **ON**: basic pipeline plus definition synthesis escalation.

The delta is the result. There is no per-goal birth ablation and no cross-theorem
promotion claim.

## Runtime Workflow

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

## Stage Roles

| Stage | LLM? | Job | Judge |
| --- | --- | --- | --- |
| `formal_ingest` | no | normalize formal input | deterministic parser |
| `decompose` | yes | propose scaffold and typed subgoals | Lean via `skeleton_check` |
| `skeleton_check` | no | check scaffold shape with `sorry` allowed | Lean skeleton mode |
| `retrieve` | no | optional cache lookup | Lean later verifies candidate |
| `synthesize` | yes | propose one subgoal proof | Lean later verifies candidate |
| `verify` | controller + repair LLM | battery, synth seed, bounded repair | Lean full mode |
| `escalation_gate` | no | route genuine normal-path stalls | blackboard state |
| `synthesize_definition` | yes | propose `def` plus bridge lemmas | cheap filters, then Lean |
| `verify_concept` | no | elaborate def and prove bridges | Lean + soundness gate |
| `prove_through` | controller + repair LLM | retry goal with verified concept in scope | Lean + soundness gate |
| `bank_concept` | no | persist accepted concept | Qdrant write status |
| `bank` | no | assemble `result.lean`, final verify, persist lemmas | Lean full mode |

## Removed Machinery

- `compare` / `lemma_compare` / `triple_log`
- `abstract` / `abstractor` / `abstracted`
- `birth_ablation`
- weak/strong prover regimes and `b_strong`
- cross-theorem promotion/pruning and `necessity_hits`

## Readout

Track:

- solved rate
- Path A/B/C wins
- reuse depth for Path A cache wins
- escalation fired
- concept verified
- prove-through solved
- final verification status
- cost and timing

Do not report A-vs-B contests, abstraction counts, weak-regime counters, or
cross-theorem necessity.
