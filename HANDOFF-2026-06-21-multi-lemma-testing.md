# Handoff: Multi-Lemma Live Testing of `lean-prove`

Date: 2026-06-21
Branch: `postwork-eval-observability`
Prereqs: see `LEAN-PROVE-PIPELINE.md` for the architecture + the 2026-06-21 changes.

## Status after the stress run

The per-sub-goal fan-out + tool-less synthesizer + parse robustness are now
**live-validated across the full 10-case matrix** below: five different multi-lemma
problems, each with a hinted and bare prompt. After the 2026-06-21 follow-up hardening,
all 10 runs reached `final_verify ok: True`.

Follow-up fixes landed during the stress run:

- `plan_contract.py`: when `options[].subtasks[]` is missing, recover sub-goals from
  scaffold `have h : T := sorry` holes. This fixed P3-bare plans that had a useful
  scaffold but no structured options.
- `lean_handlers.py`: canonicalize `.trans` chain closings such as
  `exact h2.trans h1.symm` to `exact h2`, extending the earlier `▸` closing scrub.
- `lean_handlers.py`: missing scaffold is now a real skeleton/decomposer failure, not an
  inconclusive verifier result. This prevents false-green prover runs with no final
  scaffold assembly.
- `plan_contract.py`: malformed Lean string literal scalars such as
  `lean_type: "ab" ++ "cd" = "abcd"` are escaped/quoted during YAML recovery. This fixed
  the string-conjunction cases.

Validation task IDs:

| Case | Task | Result | Closing |
| --- | --- | --- | --- |
| P1-hint | `bfb7b90b` | done, `final_verify ok` | `exact h2` |
| P1-bare | `8decb496` | done, `final_verify ok` | source had `exact h2.trans h1.symm`; assembled as `exact h2` |
| P2-hint | `acb7e3b1` | done, `final_verify ok` | `exact ⟨h1, h2⟩` |
| P2-bare | `e097f052` | done, `final_verify ok` | `exact And.intro h1 h2` |
| P3-hint | `9b1cdbd1` | done, `final_verify ok` | `exact h2` |
| P3-bare | `de6a507a` | done, `final_verify ok` | source had `▸`; assembled as `exact h2` |
| P4-hint | `a1d4af92` | done, `final_verify ok` | `exact ⟨h1, h2⟩` |
| P4-bare | `fee8cb6f` | done, `final_verify ok` | `exact And.intro h1 h2` |
| P5-hint | `60cc7df7` | done, `final_verify ok` | `exact ⟨h1, h2⟩` |
| P5-bare | `e02cb3d0` | done, `final_verify ok` | `exact And.intro h1 h2` |

Tests: `344 passed` via `agents/hyperion/.venv/bin/pytest agents/hyperion/tests`.
UI build: `npm run build` passed in `agents/hyperion-ui`.

## Environment

Both services run in Docker; `src` + `config` are volume-mounted, so reload code with
`docker restart hyperion` (no rebuild). Verify up: `curl -s localhost:4100/tasks` and
`curl -s localhost:8900/health`. Submit a run:

```bash
curl -s -X POST http://localhost:4100/tasks -H 'content-type: application/json' -d '{
  "task": "<problem text>", "workflow": "lean-prove", "hitl": "off", "cap_wall_seconds": 600
}'
# poll /tasks/{id} for status; inspect /tasks/{id}/trace for prover.subgoals + routing.dag
```

A clean pass looks like: `[expand] fanning prover chain over N sub-goals` → each
`verify__hk: discharged` → `[bank] banked N/N lemma(s)` → `final_verify ok: True`.

## The test stream (core Lean 4, no Mathlib; rfl-provable sub-goals)

Chosen so each sub-goal is `rfl`-closable (Path B can legitimately win — `decide`/`omega`
are banned from *winning*), and so the **composition** styles differ (defeq chain vs.
`And.intro`), which is exactly where the decomposer's closing line is tested.

1. **Subtraction→addition chain (defeq close).**
   "Prove in Lean 4 (core, no Mathlib) that `(20 - 5) + 3 = 18`. Decompose as
   `have h1 : 20 - 5 = 15`, `have h2 : 15 + 3 = 18`, then `exact h2`."
   *Composition:* `exact h2` (relies on `20-5` reducing to `15`). Mirrors the validated case.

2. **Arithmetic conjunction (`And.intro` close).**
   "Prove in Lean 4 (core, no Mathlib) that `2 + 2 = 4 ∧ 3 * 3 = 9`. Decompose as
   `have h1 : 2 + 2 = 4`, `have h2 : 3 * 3 = 9`, then `exact ⟨h1, h2⟩`."
   *Composition:* anonymous constructor — a different, robust closing than the chain.

3. **Mixed-operator chain (multiplication then exponent).**
   "Prove in Lean 4 (core, no Mathlib) that `2 ^ 3 + 1 = 9`. Decompose as
   `have h1 : 2 ^ 3 = 8`, `have h2 : 8 + 1 = 9`, then `exact h2`."
   *Tests:* operator variety; `2 ^ 3` reduction.

4. **Boolean logic conjunction (non-arithmetic domain).**
   "Prove in Lean 4 (core, no Mathlib) that `(true && true) = true ∧ (false || true) = true`.
   Decompose as `have h1 : (true && true) = true`, `have h2 : (false || true) = true`, then
   `exact ⟨h1, h2⟩`."
   *Tests:* Bool domain (not Nat); both sub-goals `rfl`.

5. **String concatenation conjunction (data domain).**
   "Prove in Lean 4 (core, no Mathlib) that `(\"ab\" ++ \"cd\" = \"abcd\") ∧ (\"x\" ++ \"yz\" = \"xyz\")`.
   Decompose as two `have`s, then `exact ⟨h1, h2⟩`."
   *Tests:* String append `rfl`; a third distinct domain.

> The explicit "decompose as … then …" hint is deliberate — it isolates the
> *fan-out + synthesis* path from the closing-tactic variance. Run each problem **twice**:
> once with the hint, once **without** it (just the bare proposition), to measure how often
> the decomposer's unaided closing line type-checks.

## What to record per problem

- did `skeleton_check` pass on attempt 1, or only after revision (or never)?
- the scaffold's **closing line** (the variance signal);
- `prover.subgoals[hk].discharged` + which path (A/B) for each sub-goal;
- `banked N/N` and `final_verify ok`;
- any synthesizer candidate that failed verify and why (e.g. trailing `.`, banned tactic).

## If closing/output variance bites again

**Update (2026-06-21): the mechanical fix below is landed.**
`lean_handlers.py::_canonicalize_closing` (run from `_sanitize_scaffold`, covering both
skeleton check and `bank`) rewrites `▸`-cast and `.trans` chain closings to
`exact <last_have>`, kernel-arbitrated and idempotent
(test: `test_scaffold_fragile_cast_closing_is_canonicalized`).

Decomposer closing-tactic quality is the next reliability target. Options, cheapest first:
- **Prompt:** constrain the closing line to `exact <final_have>` / `exact ⟨…⟩` / `calc`,
  explicitly forbidding `▸` term-mode casts. (Prompt-only is unreliable — we've seen it.)
- **Mechanical (DONE):** detect a `▸`/`.trans` over-clever closing in `_sanitize_scaffold` and
  rewrite the trivial defeq case to `exact <last_have>` (deterministic, like the comma scrub).
- **Structural:** have the decomposer emit only the `have` holes and let a deterministic
  closer synthesize the composition (separate native step).

## Pointers

- Pipeline + file map: `LEAN-PROVE-PIPELINE.md`
- Expansion: `crews/workflows.py::expand_per_subgoal`; runner hook in `_execute_workflow`
- Synth capture: `crews/runner.py::_capture_lemma_candidate` / `_synthesize_instruction`
- Scrubs: `lean_handlers.py::_sanitize_scaffold`, `plan_contract.py::_sanitize_frontmatter`
- Tests: `tests/test_lean_prove_workflow.py`, `tests/test_plan_contract_lean.py`
