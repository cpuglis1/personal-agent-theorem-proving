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

## Benchmark Prep / Usage Stop Handoff

Date/time: 2026-06-21 afternoon.

User asked to stop because usage was getting high. Hyperion was stopped with:

```bash
docker compose \
  -f ai-router/docker-compose.yml \
  -f agents/hyperion/docker-compose.override.yml \
  -f agents/hyperion/docker-compose.lean.yml \
  stop hyperion
```

No final `eval_mode=test` run was launched.

### What landed before this handoff

Committed baseline:

- `d998ab2 Add benchmark eval modes and verifier profiles`
  - added `eval_mode=train|dev|test`
  - disables persistent lemma/concept/episode writes in `dev` and `test`
  - added `lean_profile=core|mathlib`
  - writes `artifacts/eval_manifest.json`
  - added benchmark helper `hyperion.eval.lean_prove_benchmark`
  - added trainer/dev/test split staging under `agents/hyperion/evals/`

Uncommitted follow-up changes made during benchmark prep:

- `agents/hyperion/src/hyperion/crews/lean_handlers.py`
  - `_sanitize_lean_source(..., profile=...)` now preserves `import Mathlib` when
    `profile=mathlib`, while keeping core behavior unchanged.
  - repair prompts now include the verifier profile.
- `agents/hyperion/src/hyperion/crews/runner.py`
  - `_synthesize_instruction` now tells Path B whether the active verifier profile is
    `core` or `mathlib`.
- `agents/hyperion/config/agents/decomposer.json`
  - decomposer prompt now says core is default, but `lean_profile=mathlib` allows
    Mathlib imports/tactics/syntax.
- `agents/hyperion/config/agents/lemma_synthesizer.json`
  - lemma synthesizer prompt now permits `import Mathlib` only when the task instruction
    says the verifier profile is `mathlib`.
- `agents/hyperion/evals/lean_prove_splits/dev_mathlib.jsonl`
  - new small public miniF2F-derived dev split using `formal/valid.lean`, not `test.lean`.

Focused verification after the profile-aware edits:

```text
agents/hyperion/.venv/bin/pytest \
  agents/hyperion/tests/test_lean_prove_workflow.py \
  agents/hyperion/tests/test_lean_verify.py

52 passed
```

Full backend suite and UI build were not rerun after these last uncommitted prompt/profile edits.

### Dev benchmark results actually completed

Important interpretation rule:

`final_verify.ok=true` means the assembled `result.lean` is valid Lean for the theorem
Hyperion assembled: it type-checks, has no `sorry`, and the Lean kernel accepts the proof.
It does **not** independently prove that the decomposer formalized the original natural
language problem correctly, or that it matched the intended benchmark statement. For exact
formal benchmark prompts, this is close to "solved"; for natural-language prompts, also
check the formalized proposition against the expected statement.

Core dev pass was run with:

```bash
agents/hyperion/.venv/bin/python -m hyperion.eval.lean_prove_benchmark \
  --cases agents/hyperion/evals/lean_prove_splits/dev.jsonl \
  --eval-mode dev \
  --out agents/hyperion/tasks/benchmark-core-dev-results.jsonl \
  --order-seed 1 \
  --poll-seconds 5
```

Results:

| Case | Task | Mode | Profile | Result |
| --- | --- | --- | --- | --- |
| `core-dev-exp-chain-001` | `8265b9d3` | `dev` | `core` | done, `final_verify.ok=true`, 2 subgoals |
| `core-dev-string-conj-001` | `b1befa83` | `dev` | `core` | done, `final_verify.ok=true`, 2 subgoals |

Result file:

```text
agents/hyperion/tasks/benchmark-core-dev-results.jsonl
```

This is the successful "Core Lean, no Mathlib, no writes" sanity pass: 2/2.

### Mathlib dev attempt stopped early

Mathlib dev pass was started with:

```bash
agents/hyperion/.venv/bin/python -m hyperion.eval.lean_prove_benchmark \
  --cases agents/hyperion/evals/lean_prove_splits/dev_mathlib.jsonl \
  --eval-mode dev \
  --out agents/hyperion/tasks/benchmark-mathlib-dev-results.jsonl \
  --order-seed 1 \
  --poll-seconds 5
```

It was interrupted for usage. The helper process was then killed, but it had already
submitted a second backend task. Hyperion was stopped to prevent further LLM calls.

Observed rows:

| Case | Task | Mode | Profile | Result |
| --- | --- | --- | --- | --- |
| `miniF2F-valid-mathd-algebra-182` | `6d5fad7f` | `dev` | `mathlib` | failed at skeleton revision budget |
| `miniF2F-valid-mathd-algebra-462` | `27166865` | `dev` | `mathlib` | stopped mid-run by stopping Hyperion |

Result file:

```text
agents/hyperion/tasks/benchmark-mathlib-dev-results.jsonl
```

Important failure signal: Mathlib verification plumbing is reachable, but the decomposer
does not yet robustly handle prompts containing a full top-level theorem command plus
`import Mathlib`. The first miniF2F case failed before retrieve/synthesize, during
`skeleton_check` revisions, with malformed scaffold/type output.

Next best fix before resuming Mathlib:

- change Mathlib benchmark prompts to pass the bare proposition/context rather than an
  entire `import Mathlib` + `theorem ... := by sorry` command, or
- teach the decomposer/skeleton wrapper to support full top-level theorem commands and
  extract the theorem proposition safely.

Cheapest immediate path: regenerate `dev_mathlib.jsonl` prompts as propositions, e.g.
`Prove in Lean 4 with lean_profile=mathlib, assuming (y : Complex), that ...`, and keep
`formal_statement` as the original miniF2F theorem for provenance.

### Current stop state

- Hyperion container is stopped intentionally.
- Lean sidecar and other infra may still be up.
- There is an untracked generated directory from the Compose-mounted task volume:
  `ai-router/tasks/`.
- `ai-router/tasks/27166865` is a partial stopped Mathlib task. Do not count it as a
  benchmark result.
- Do not launch `eval_mode=test` without explicit user go-ahead.

### Suggested resume sequence

1. Review/commit or revise the uncommitted profile-aware Mathlib prompt/sanitizer changes.
2. Replace `dev_mathlib.jsonl` prompts with proposition-style prompts.
3. Run local tests:

```bash
agents/hyperion/.venv/bin/pytest agents/hyperion/tests
npm run build --prefix agents/hyperion-ui
```

4. Restart Hyperion only when ready to spend:

```bash
docker compose \
  -f ai-router/docker-compose.yml \
  -f agents/hyperion/docker-compose.override.yml \
  -f agents/hyperion/docker-compose.lean.yml \
  up -d hyperion
```

5. Re-run only Mathlib dev first. Do not run final test.
