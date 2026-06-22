# Lean-Prove Benchmark Splits

This directory is the benchmark staging area. It is intentionally split by evaluation
discipline:

- `smoke.jsonl`: self-authored curated cases for CI and fast wiring checks only.
  These are not a progress metric and must not be reported as held-out dev signal.
- `train.jsonl`: trainer/calibration cases. Runs may use `eval_mode=train`, which allows
  lemma/concept/episode writes.
- `dev.jsonl`: public development/validation cases. Runs should use `eval_mode=dev`,
  which keeps artifacts and traces but disables persistent learning writes. This file must
  not contain self-authored, rfl-tuned, or prompt-tuned cases.
- `dev_mathlib.jsonl`: public miniF2F Lean 4 `formal/valid.lean` development cases,
  run with `eval_mode=dev` and `lean_profile=mathlib`. `dev.jsonl` currently mirrors this
  public Mathlib slice so the default dev path is not self-authored.
- `test.jsonl`: final held-out cases. Runs must use `eval_mode=test`, with frozen code,
  prompts, model aliases, retrieval index, bank snapshot, and verifier profile.

Do not launch a final test run until the trainer/dev protocol and frozen retrieval snapshot
are explicitly approved.

Self-authored cases can gate regressions, but they cannot count as benchmark progress.
Promote a case to dev only when it comes from an external/public source or a frozen
pre-registered holdout pool, and only before any prompt/scaffold tuning against that case.

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
