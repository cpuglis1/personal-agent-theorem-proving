# Lean-Prove Evaluation Protocol

## Modes

Hyperion tasks accept `eval_mode`:

- `train`: persistent learning writes enabled. `bank` may write lemmas, `bank_concept`
  may write concepts, and the API may write episodic memory.
- `dev`: artifacts/traces enabled, persistent learning writes disabled.
- `test`: same no-write behavior as `dev`; use only after code, prompts, models,
  retrieval index, and bank snapshots are frozen.

The rule is: verification artifacts are always written; learning artifacts are
mode-gated.

## Verifier Profiles

Tasks also accept `lean_profile`:

- `core`: rejects Lean `import` statements and preserves the no-Mathlib surface.
- `mathlib`: allows `import Mathlib` on the warm-cache Lean sidecar project.

The current sidecar image already builds a Mathlib-backed Lake project. The profile flag
is the policy switch that determines whether a task may use that dependency.

## Split Discipline

Use `evals/lean_prove_splits/smoke.jsonl` only for fast wiring checks; do not report it
as benchmark progress.
Use `evals/lean_prove_splits/train.jsonl` with `eval_mode=train`.
Use `evals/lean_prove_splits/dev.jsonl` with `eval_mode=dev`; dev must be public or
pre-registered held-out data, not self-authored cases tuned during development.
Do not run `test.jsonl` until the held-out set and frozen retrieval snapshot are approved.

The helper runner refuses `--eval-mode test` by default:

```bash
python -m hyperion.eval.lean_prove_benchmark \
  --cases agents/hyperion/evals/lean_prove_splits/dev.jsonl \
  --eval-mode dev \
  --out agents/hyperion/tasks/dev-results.jsonl
```

## Manifest

Every `run_task` writes `artifacts/eval_manifest.json` with:

- git commit and dirty flag
- eval mode and learning-write flag
- verifier profile and sidecar config hashes
- model aliases
- retrieval collection names/config hash
- agent/workflow/model config hashes
- caps and benchmark metadata
