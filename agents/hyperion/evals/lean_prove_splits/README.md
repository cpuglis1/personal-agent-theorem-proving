# Lean-Prove Benchmark Splits

This directory is the benchmark staging area. It is intentionally split by evaluation
discipline:

- `train.jsonl`: trainer/calibration cases. Runs may use `eval_mode=train`, which allows
  lemma/concept/episode writes.
- `dev.jsonl`: development/validation cases. Runs should use `eval_mode=dev`, which
  keeps artifacts and traces but disables persistent learning writes.
- `dev_mathlib.jsonl`: development/validation cases from public miniF2F Lean 4
  `formal/valid.lean`, run with `eval_mode=dev` and `lean_profile=mathlib`.
- `test.jsonl`: final held-out cases. Runs must use `eval_mode=test`, with frozen code,
  prompts, model aliases, retrieval index, bank snapshot, and verifier profile.

Do not launch a final test run until the trainer/dev protocol and frozen retrieval snapshot
are explicitly approved.

## JSONL Schema

Each line is one object:

```json
{
  "id": "miniF2F-core-train-0001",
  "source": "miniF2F-derived",
  "split": "train",
  "lean_profile": "core",
  "workflow": "lean-prove",
  "prompt": "Prove in Lean 4 ...",
  "formal_statement": "example : ... := by sorry",
  "expected": "solvable",
  "tags": ["nat", "conjunction"]
}
```

Use `lean_profile=mathlib` for cases that require `import Mathlib`; otherwise keep
`lean_profile=core`.
