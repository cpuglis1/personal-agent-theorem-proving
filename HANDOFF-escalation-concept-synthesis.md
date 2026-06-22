# Handoff: Escalation / Concept-Synthesis — the real next bottleneck

Date: 2026-06-22
Branch: `concept-synthesis`

## TL;DR

The threaded-subgoal **final-assembly bug is fixed** (and generalized), the **dev split is
now 3/3**, and the **definition-escalation (Path C) branch was exercised live for the first
time**. The branch *fires end-to-end* but **rescues nothing**: concept synthesis produces
**degenerate abstractions** that rename the goal / restate hypotheses, and `verify_concept`
lets them through because it gates on "the `def` type-checks", not "the concept discharges
the stalled subgoal". That is the next substantial piece of work.

There is **no genuine `escalation_on_only` fixture yet** — nothing actually rescues. Do not
manufacture one by weakening the battery/synth.

## What was done this session (commits on `concept-synthesis`)

- `c889302` **Thread implicit parent binders as explicit in subgoal ∀-type** — the core fix.
- `3563b8f` **Submit exact `formal_statement` (not prose) in lean-prove benchmark.**
- `78de2b4` **Correct hard-smoke expectation to `both_pass`** (cases don't exercise escalation).
- `1f181c2` **Parse Unicode binder names** (Greek/primed/subscripted).

### 1. Threaded final-assembly bug (FIXED — was the trim-DAG handoff's open item)

The previous handoff (`HANDOFF-trim-dag-off-on-smoke.md`) proposed wrapping the `by` block in
extra parens. **That fix was wrong** — verified against the live Lean sidecar it produced the
*identical* `application type mismatch`.

Real root cause: `_threaded_goal_type_from_formal` (`agents/hyperion/src/hyperion/crews/lean_handlers.py`)
built the threaded ∀-type from each binder's `raw` form, so an **implicit/instance** parent
binder (`{A : Type}`, `{x y : A}`, `[inst : C A]`) stayed implicit. But `_proof_body_for_hole`
instantiates the threaded proof **positionally** (`((proof : ∀…)) A P x y …`), and Lean rejects
positional explicit args against implicit binders (each arg slides onto the wrong slot).

Fix: new `_explicit_binder()` renders every threaded binder explicitly as `(names : type)` so the
∀ binder order matches positional instantiation 1:1. Verified live: the previously-failing
`hard_predicate_transport` / `hard_relcomp_identity_left` assemblies now elaborate, and the fix
generalizes across 6 binder shapes (all-implicit, mixed, instance, dependent, all-explicit).

Regression test: `tests/test_lean_prove_workflow.py::test_bank_threads_implicit_parent_binders_as_explicit`.

### 2. Unicode binder parser gap (FIXED — found while validating #1)

`lean_statement._IDENT_RE` / `lean_handlers._LEAN_IDENT_RE` were ASCII-only, so non-ASCII binder
names (`{α : Type}`) were silently dropped from `local_context` and never threaded (masked until
now by Lean autobound implicits). Broadened both, plus the ∀-binder pattern, to `[^\W\d][\w']*`.
Regression test: `tests/test_lean_statement.py::test_parse_formal_statement_unicode_binder_names`.

### 3. Hard-smoke fixture relabel (`escalation_on_only` → `both_pass`)

Paired OFF/ON re-run after the assembly fix: all 3 `hard_smoke.jsonl` cases close under normal
proving (Path B) in **both** regimes — `final_verify ok`, `path_c_wins=0`, `escalations=0`,
`rescued=False`. They never reach the escalation gate, so `escalation_on_only` was false.
Updated the fixture, `tests/test_eval.py::test_hard_smoke_fixture_schema`, and documented the
gap in `evals/EVAL-PROTOCOL.md`.

## Validation runs (all via the live Hyperion API at `http://localhost:4100`)

The API container mounts host `agents/hyperion/src` → `/app/src`; after editing code,
`docker restart hyperion` to reload the process.

- **Dev split (`evals/lean_prove_splits/dev.jsonl`, eval_mode=dev, default regime): 3/3.**
  182 ✓ (2 subgoals discharged + assembled — the prior bottleneck is GONE), 462 ✓, 132 ✓; all
  via normal proving (`path_c_wins=0`, `escalations=0`). No regression from the shared
  `_threaded_goal_type_from_formal` change.
- **Hard smoke paired OFF/ON: 3/3 both regimes** (see relabel above).
- **Unit/integration:** `tests/test_lean_prove_workflow.py tests/test_eval.py
  tests/test_lean_statement.py` all green.

## Escalation discovery pilot (the finding)

Goal: find a *genuine* normal-stalling case that escalation rescues, to earn a real
`escalation_on_only` fixture (the curated hard-smoke ones don't stall). Method — a cheap
two-phase loop using **existing** tooling, no battery/synth weakening:

1. Converted miniF2F-valid (`/private/tmp/miniF2F-lean4-hyperion/formal/valid.lean`) → 244-case
   benchmark jsonl (`agents/hyperion/tasks/minif2f_valid_pool.jsonl`, all parse-clean).
2. Ran 20 (strided sample) with escalation **OFF** (`prover_definition_escalation=False`,
   single mode) to harvest stalls.
3. Re-ran 6 stalls with escalation **ON**.

Discovery artifacts (gitignored, host `agents/hyperion/tasks/`):
`minif2f_valid_pool.jsonl`, `minif2f_discovery20_off.jsonl`, `minif2f_discovery_on.jsonl`,
and the timestamped `minif2f-discovery20-off-*.jsonl` / `minif2f-discovery-on-*.jsonl` results.
Classify with `scripts/lean_prove_failure_classifier.py` (read-only; the target bucket is
"proof sourcing / downstream stall" = `normal_prover_stall_after_skeleton`).

### Results

**OFF harvest:** 6/20 closed by normal proving; **14/20 were clean normal-prover stalls**
(`cannot assemble result.lean; undischarged sub-goal(s)`). Zero infra errors, zero
binder-threading-ceiling (`subgoal_unbound_context` empty everywhere), zero assembly bugs — the
#1/#2 fixes hold on the harder pool; these are honest "couldn't source the proof".

**ON re-run (6 stalls):** the branch **fires** — `escalations=1` on 5/6, `concepts_verified=1`
on 4/6 (gate → abstract → synthesize_definition → verify_concept all execute). **But
`path_c_wins=0` and `discharged=null` everywhere — zero genuine rescues.** The one failed→done
flip (`amc12a_2009_p2`) had `esc=0` and a *different* decomposition (3 subgoals → 1) — that's
decomposition **nondeterminism**, not an escalation rescue; do **not** label it
`escalation_on_only`.

### Root cause: degenerate concept synthesis (the next bottleneck)

Every "verified" concept just renames the goal or restates hypotheses, with no proof power:

| case | stalled goal | synthesized `def` | pathology |
|---|---|---|---|
| algebra_393 | `σ.2 33 = 2` | `def CubicEquiv … : Prop := σ.2 33 = 2` | renames the goal |
| algebra_192 | `q*e*d = 292*I` | `def ComplexFactors (q e:ℂ):ℂ×ℂ := (q,e)` | trivial / irrelevant |
| numbertheory_405 | `(t a+t b+t c)%7=5` | `def …:Prop := a≡5 ∧ b≡10 ∧ c≡15 [MOD 16]` | restates hyps |
| algebra_422 | `x=47/24` | `def EquivCondition … := σ.1(47/24)=…` | restates a computation |

They pass `verify_concept_handler` (`lean_handlers.py`) because it only checks: definition
elaborates (no `sorry`) + an **optional** vacuity probe (`example := by trivial` must fail) +
each bridge closes **in isolation**. It **never requires the concept to discharge the actual
stalled hole** — and a `def Foo : Prop := <goal>` always elaborates, so the gate is satisfied
without proof power.

## Recommended next work (for the new window)

1. **Synthesis must emit a discharging lemma**, not a goal-renaming `def`: a *general, reusable*
   proposition **with a proof** whose application closes the stalled subgoal (or from which it
   follows). Look at `synthesize_definition_handler` / `abstract` and their prompts in
   `lean_handlers.py` (search `synthesize_definition`, `abstract`, `_concept_`).
2. **Gate on discharge.** `verify_concept` (and/or `birth_ablation`) must require the concept to
   actually close the stalled hole — re-prove the subgoal *through* the concept and demand a
   `discharged` Path-C winner — not merely that the `def`/bridges type-check.
3. **De-confound OFF vs ON.** Pin/seed decomposition so the paired smoke is controlled;
   today decomposition nondeterminism can flip a case independent of escalation.
4. Only after a real rescue exists: promote that case into a genuine `escalation_on_only`
   fixture (earned from a real failure, not hand-authored).

## Guardrails (carried from prior handoffs — still apply)

- Don't weaken/disable the deterministic battery or synth to force Path C — that recreates the
  intentionally-cut weak/strong regime.
- Infra-down ≠ proof-failure: route on `infra_ok` before trusting `ok` (see `tools/lean_verify.py`).
- The kernel (final bank verify) is the only arbiter; an LLM proposal isn't "verified" until it
  type-checks.

## Relevant files

- `agents/hyperion/src/hyperion/crews/lean_handlers.py` — `_threaded_goal_type_from_formal`,
  `_explicit_binder`, `_proof_body_for_hole`, `escalation_gate_handler`,
  `synthesize_definition_handler`, `verify_concept_handler`.
- `agents/hyperion/src/hyperion/crews/lean_statement.py` — `parse_formal_statement`, `_IDENT_RE`.
- `agents/hyperion/src/hyperion/eval/lean_prove_benchmark.py` — paired/single runner.
- `agents/hyperion/scripts/lean_prove_failure_classifier.py` — read-only stall classifier.
- `agents/hyperion/evals/lean_prove_splits/` — `dev.jsonl`, `hard_smoke.jsonl`; `EVAL-PROTOCOL.md`.
