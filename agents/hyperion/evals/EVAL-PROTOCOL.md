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
Use `evals/lean_prove_splits/hard_smoke.jsonl` only for paired OFF/ON definition
escalation smoke; it is self-authored and exists to exercise the branch, not to report
headline theorem-proving progress.
NOTE (2026-06-22): the current curated cases all close under normal proving (Path B) in
BOTH regimes, so their `expected` is `both_pass`, not `escalation_on_only` — they no
longer force the escalation gate. They remain useful as an end-to-end assembly/parity
smoke (they regression-guard the threaded-subgoal final-assembly fix). To actually
exercise OFF/ON escalation we still need genuinely normal-stalling cases sourced from
real failures; do NOT manufacture a stall by weakening the battery or synth (that would
recreate the intentionally-cut weak/strong regime).
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

Paired definition-escalation smoke:

```bash
python -m hyperion.eval.lean_prove_benchmark \
  --cases agents/hyperion/evals/lean_prove_splits/hard_smoke.jsonl \
  --eval-mode dev \
  --out agents/hyperion/tasks/hard-smoke-off-on.jsonl \
  --paired-off-on
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
