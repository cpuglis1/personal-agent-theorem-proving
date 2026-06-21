# Handoff: Trace Flow + Decompose/Skeleton Gate

Date: 2026-06-21

## Current State

The Trace Flow UI and prover control path were modified so the run is more inspectable and so invalid decompositions no longer silently fall through to synthesis/bank.

The most recent live probe was:

- Task: `cd735778`
- Prompt: GSM8K-style Maria stickers problem, formalize/prove final number is 15
- Terminal status: `failed`
- Latest error:

```text
cannot assemble result.lean; undischarged sub-goal(s): h2
```

What happened:

- First decomposer attempt failed `skeleton_check`.
- Skeleton failure correctly revised the decomposer instead of entering retrieve/synthesize.
- Second decomposer attempt passed `skeleton_check`.
- The run entered retrieve/synthesize/verify.
- Path B discharged `h1`.
- Bank failed because the decomposer produced a selected plan with two active subtasks, `h1` and `h2`, but the shipped `lean-prove` DAG currently has only one active retrieve/synthesize/verify chain.

This is a correct failure mode now: the old behavior would have been more likely to report a false green.

## Important Latest Plan Output

The second decomposer plan for `cd735778` looked like:

```yaml
scaffold: "example : 18 - 7 + 4 = 15 := by\n  have h1 : 18 - 7 = 11 := sorry\n  have\
  \ h2 : 11 + 4 = 15 := sorry\n  exact h2\n"
options:
- id: a
  subtasks:
  - id: h1
    lean_type: 18 - 7 = 11
  - id: h2
    lean_type: 11 + 4 = 15
selected_option: a
```

The next fix should either:

- make the decomposer reliably emit one final-proposition subgoal for the current single-chain `lean-prove` DAG, or
- restore/build dynamic per-subgoal DAG expansion so multi-subgoal decompositions are actually executable.

The final edit before stopping added a stronger instruction directly to `_plan_task` telling the decomposer that the current shipped `lean-prove` DAG has one active subgoal chain and simple arithmetic should use:

```lean
have h1 : <final proposition> := sorry
exact h1
```

That last edit was not retested because the user asked to stop.

## Code Changes Made

### Correctness / DAG Behavior

- `agents/hyperion/config/workflows/lean-prove.json`
  - Changed `decompose` back from native `lean_decompose` to a model-backed `plan` node using agent `decomposer`.

- `agents/hyperion/src/hyperion/crews/runner.py`
  - Added skeleton failure revision loop:
    - if `skeleton_check` returns `ok: false`, rerun plan/decompose with Lean diagnostics
    - retry up to `_MAX_REVISIONS`
    - do not continue to retrieve/synthesize on skeleton failure
  - Added overwrite support to `_write_fallback_plan(..., overwrite=True)` for revision outputs.
  - Strengthened `_plan_task` prompt with the current single-chain `lean-prove` constraint.

- `agents/hyperion/src/hyperion/crews/lean_handlers.py`
  - `skeleton_check_handler` now writes `skeleton_errors`.
  - `bank_handler` now writes `final_verify`.
  - `bank_handler` now raises `ProofFailed` if assembled `result.lean` fails final verification instead of returning a false green.

- `agents/hyperion/config/agents/decomposer.json`
  - Strengthened decomposer prompt:
    - natural-language exam problems must first be formalized as Lean propositions
    - no raw English Lean types
    - no value declarations as subgoals
    - no Mathlib tactics like `norm_num`
    - simple arithmetic should use one final-proposition subgoal

### Trace / UI Observability

- `agents/hyperion/src/hyperion/eval/trace.py`
  - Trace payload now includes:
    - `skeleton_errors`
    - `final_verify`
    - earlier added: concept context and stall errors

- `agents/hyperion/src/hyperion/usage.py`
  - LLM trace model field now prefers the response model name when available, falling back to the requested model/alias.

- `agents/hyperion-ui/src/api/client.ts`
  - Added native node support and handler/prover trace typing.

- `agents/hyperion-ui/src/pages/TraceFlow.tsx`
  - Detail sidebar separates:
    - native module input/output
    - model called
    - handler
    - LLM prompt/response
  - Native nodes label model as:
    - `none - native Python handler`
  - Worker/model nodes show recorded model.
  - Module I/O now includes context keys read/written, verifier response, selected candidate, and failure reason where available.

- `agents/hyperion-ui/src/pages/ProverRun.tsx`
  - Changed wording from `Data source / Live (:4100)` to clearer trace-source wording.
  - Scaffold section now shows decomposer output and subgoal types.

- `agents/hyperion/src/hyperion/server/api.py`
  - `/runs` now includes local task-dir runs, so CLI theorem stream runs are visible.

## Validation Completed Before Last Untested Prompt Edit

These passed before the final `_plan_task` prompt-strengthening edit:

```text
cd agents/hyperion && .venv/bin/uv run pytest tests/test_lean_prove_workflow.py tests/test_eval.py tests/test_prover_trace_surface.py -q
38 passed
```

```text
cd agents/hyperion-ui && npm run build
passed
```

After adding the `_plan_task` prompt constraint, a focused test run was started but interrupted by the user. Do not count that final prompt edit as tested.

## Live Probe History

- `3b779e3c`: original GSM probe before fixes
  - status `done`, but invalid `result.lean`
  - verifier rejected final artifact
  - exposed false-green bank/final verification bug

- `1d2e36c1`: after moving decompose back to model plan and adding skeleton revision
  - status `failed`
  - failed at skeleton after two revisions
  - did not enter retrieve/synthesize

- `d7d72809`: after first decomposer prompt tightening
  - status `failed`
  - still failed skeleton due invalid multi-step scaffold syntax

- `cd735778`: after stricter decomposer prompt
  - first scaffold failed, revision passed skeleton
  - synthesis/verify discharged `h1`
  - bank failed because `h2` was undischarged
  - latest error: `cannot assemble result.lean; undischarged sub-goal(s): h2`

## Uncommitted Context

There are uncommitted changes across backend, UI, workflow config, decomposer config, tests, and the prior `concept_stream.py` review artifact. Do not assume a clean worktree.

