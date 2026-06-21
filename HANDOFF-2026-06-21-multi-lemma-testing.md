# Handoff: Multi-Lemma Live Testing of `lean-prove`

Date: 2026-06-21
Branch: `postwork-eval-observability`
Prereqs: see `LEAN-PROVE-PIPELINE.md` for the architecture + the 2026-06-21 changes.

## Goal of the next session

The per-sub-goal fan-out + tool-less synthesizer + parse robustness are landed and
**live-validated on one problem** (`13b6236d`, `18 - 7 + 4 = 15` → banked 2/2,
`final_verify ok`). Now stress the path across **3–5 distinctly different multi-lemma
problems** to (a) confirm the fan-out generalizes and (b) characterize the one open issue —
**decomposer closing-tactic variance** (sometimes a clean `exact h2`, sometimes a fragile
`exact h2.trans (h1.symm ▸ rfl)` that fails skeleton).

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

## If the open issue bites (it will, without the hint)

**Update (2026-06-21): the mechanical fix below is landed.**
`lean_handlers.py::_canonicalize_closing` (run from `_sanitize_scaffold`, covering both
skeleton check and `bank`) rewrites a `▸`-cast closing tactic to `exact <last_have>`,
kernel-arbitrated and idempotent (test: `test_scaffold_fragile_cast_closing_is_canonicalized`).
The remaining options stay listed in case the fan-out stress run surfaces a closing the
mechanical scrub doesn't cover.

Decomposer closing-tactic quality is the next reliability target. Options, cheapest first:
- **Prompt:** constrain the closing line to `exact <final_have>` / `exact ⟨…⟩` / `calc`,
  explicitly forbidding `▸` term-mode casts. (Prompt-only is unreliable — we've seen it.)
- **Mechanical (DONE):** detect a `▸`/over-clever closing in `_sanitize_scaffold` and
  rewrite the trivial defeq case to `exact <last_have>` (deterministic, like the comma scrub).
- **Structural:** have the decomposer emit only the `have` holes and let a deterministic
  closer synthesize the composition (separate native step).

## Pointers

- Pipeline + file map: `LEAN-PROVE-PIPELINE.md`
- Expansion: `crews/workflows.py::expand_per_subgoal`; runner hook in `_execute_workflow`
- Synth capture: `crews/runner.py::_capture_lemma_candidate` / `_synthesize_instruction`
- Scrubs: `lean_handlers.py::_sanitize_scaffold`, `plan_contract.py::_sanitize_frontmatter`
- Tests: `tests/test_lean_prove_workflow.py`, `tests/test_plan_contract_lean.py`
