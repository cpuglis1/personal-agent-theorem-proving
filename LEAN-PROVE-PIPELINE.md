# Lean-Prove Pipeline — Architecture & 2026-06-21 Hardening

How the `lean-prove` workflow turns a natural-language (or formal) goal into a
kernel-verified, sorry-free `result.lean`, and what changed on the
`postwork-eval-observability` branch to make multi-lemma decompositions actually
executable and observable.

## The DAG (shipped single chain)

`config/workflows/lean-prove.json`:

```
decompose ─▶ skeleton_check ─▶ ┌ retrieve ┐                ┌ compare ─▶ abstract ─┐
            (type-check the   │           ├─▶ verify ─▶────┤                      ├─▶ bank
             have-chain in    └ synthesize┘  (native       └ escalation_gate ─▶ … ─┘
             skeleton mode)                   controller)     synthesize_definition
                                                              verify_concept
                                                              birth_ablation
                                                              bank_concept
```

- **decompose** (agent `decomposer`): writes `plan.md` — a YAML front-matter contract
  with a `scaffold` (a `have <id> : <prop> := sorry` chain that composes to the target)
  plus `options[].subtasks[]` carrying each sub-goal's exact Lean `lean_type`.
- **skeleton_check** (native): type-checks the scaffold in *skeleton* mode (`sorry`
  allowed) against the real Lean kernel (sidecar at `:8900`). A real failure revises the
  decomposer (≤2 passes) instead of falling through to a false green.
- **retrieve** (native, Path A) ‖ **synthesize** (agent `lemma_synthesizer`, Path B): two
  proof sources for a sub-goal run in one wave.
- **verify** (native): the controller — the kernel is the verdict. Picks the winning
  candidate, runs the bounded repair loop, writes `discharged:<sg>`.
- **compare → abstract** (normal win) or **escalation_gate → … → bank_concept** (stall →
  definition synthesis).
- **bank** (native): assembles the sorry-free `result.lean` from scaffold + discharged
  sub-goals, **full-verifies it** (final ground truth), and banks each winning lemma.

The blackboard is sub-goal-namespaced (`candidate_b:<sg>`, `discharged:<sg>`, …), so the
chain can be cloned per sub-goal over one shared store.

## What changed (2026-06-21)

The trigger: a GSM-style probe (`cd735778`) cost ~6 LLM calls on the decomposer and
failed with `undischarged sub-goal(s): h2` — the decomposer emitted a 2-sub-goal plan
but the shipped DAG only ran one chain.

### 1. Tool-less decomposer — kill the ReAct format loop
`config/agents/decomposer.json`: `tools: []`, `max_iter: 1`, output contract = "return the
full `plan.md` as your final answer." A CrewAI agent with no tools makes one LLM call and
returns a final answer — no `Thought/Action` loop, so the `Invalid Format: missed 'Action:'`
re-prompts (which inflated each activation to ~3 calls) are gone. `_write_fallback_plan`
already materialized `plan.md` from the raw output, so `workspace_write` was redundant.
**Live:** decomposes in one pass ("I now can give a great answer", zero format errors).

### 2. Per-sub-goal DAG expansion — multi-`have` scaffolds become executable
`crews/workflows.py::expand_per_subgoal` clones every template node strictly between
`skeleton_check` and `bank` once per active sub-goal (`<node>__<sg>`), each carrying its
sub-goal id in `instruction`; `bank` fans in over all clones. Wired into
`crews/runner.py::_execute_workflow`: after `skeleton_check` passes with >1 active
sub-goal, the runner re-enters with the fanned workflow (an `expanded` guard prevents
double-expansion; already-hand-fanned workflows are detected and skipped). Single-sub-goal
plans and non-prover workflows are untouched. `bank`'s strict "all sub-goals required"
check is now *correct* — the DAG genuinely discharges every sub-goal.

### 3. Tool-less synthesizer + runner-owned capture — robust Path-B hand-off
`config/agents/lemma_synthesizer.json`: `tools: []`, `max_iter: 1`. The runner owns the
per-sub-goal prompt (`_synthesize_instruction`, with the exact `lean_type` injected so the
agent never reads `plan.md`) and **deterministically captures** the agent's final-answer
JSON into `candidate_b:<sg>` (`_capture_lemma_candidate` + `_extract_json_object`, which is
string-aware so braces inside Lean source don't fool it). Previously the agent
nondeterministically called `workspace_write` instead of `context_put`, so its candidate
never reached the blackboard and that sub-goal stalled.

### 4. Mechanical scaffold/plan scrubs — survive decomposer dialect tics
- `lean_handlers.py::_sanitize_scaffold`: strips Lean-3 trailing-comma tactic separators
  (`:= sorry,`) inside `_scaffold_as_command` (covers skeleton + bank).
- `plan_contract.py::_sanitize_frontmatter`: now quotes unquoted-colon scalars at **any
  indent** (was column-0 only) and is block-scalar-body aware. A nested option `summary:`
  with a colon ("two steps: subtraction…") had been sinking the *whole* plan → options
  dropped → no fan-out.

### 5. Observability — every stage inspectable in Trace Flow
`eval/trace.py`, `server/api.py`, `usage.py`, and `hyperion-ui/` (TraceFlow, ProverRun,
client): native stages are recorded per `node_id`; `/tasks/{id}/trace` returns the fanned
`routing.dag`, per-node events, and a plan-driven `prover.subgoals` panel (each sub-goal's
candidates, verdict, discharge, stall reason). So a fanned run shows all `__<sg>` nodes and
per-sub-goal status.

## Validation

- **344 tests pass** (`agents/hyperion/.venv/bin/pytest agents/hyperion/tests`), incl.
  runtime fan-out e2e, deterministic candidate capture, scaffold/colon/string-literal
  YAML scrubs, scaffold-only subtask recovery, and missing-scaffold failure behavior.
- **Live 10-case matrix passes** (hinted + bare prompts for arithmetic chains,
  arithmetic conjunction, exponent chain, boolean conjunction, and string conjunction).
  All reached `final_verify ok: True`; task IDs are recorded in
  `HANDOFF-2026-06-21-multi-lemma-testing.md`.
- **Live, kernel-verified** (task `13b6236d`, "18 − 7 + 4 = 15"): fan-out over h1/h2, both
  discharged via Path B, `banked 2/2 lemma(s)`, `final_verify ok: True`:
  ```lean
  example : 18 - 7 + 4 = 15 := by
    have h1 : 18 - 7 = 11 := rfl
    have h2 : 11 + 4 = 15 := rfl
    exact h2
  ```

## Decomposer closing-tactic variance — mechanically canonicalized

The sub-goal *decomposition* was already reliable; the scaffold's **closing line** was not.
The decomposer alternated between the correct `exact h2` and a fragile
`exact h2.trans (h1.symm ▸ rfl)` whose `▸` cast failed skeleton (the revision budget then
gave up). Fixed with the same mechanical, kernel-arbitrated approach as the comma scrub:
`lean_handlers.py::_canonicalize_closing` (run from `_sanitize_scaffold`, so it covers both
skeleton check and `bank` assembly) rewrites a `▸`-cast or `.trans` chain closing tactic —
the chain's last non-blank line — to the canonical `exact <last_have>`. Clean chain closes
(`exact h2`) and conjunction closes (`exact ⟨h1, h2⟩` / `exact And.intro h1 h2`) pass through
untouched, and the scrub is idempotent. The kernel still arbitrates (skeleton + final
`bank` verify), so it can only swap a known-fragile closing for the canonical one, never
manufacture a false green.

## Scaffold contract recovery

Two decomposer contract gaps surfaced in the matrix and are mechanically covered:

- If the decomposer emits a useful scaffold but omits `options[].subtasks[]`,
  `PlanFrontmatter.active_subtasks()` recovers typed sub-goals from the scaffold's
  `have h : T := sorry` holes so fan-out still runs.
- If the decomposer emits Lean string literal expressions as malformed YAML scalars
  (`lean_type: "ab" ++ "cd" = "abcd"`), `_sanitize_frontmatter` escapes and quotes the
  whole Lean expression so the scaffold/options are not dropped.

Missing scaffold is now a real skeleton/decomposer failure (`ok=False`) rather than an
inconclusive verifier result; this prevents false-green runs that bank a fallback single
lemma without a final scaffold theorem.

## Constraints worth knowing for new test goals

- Verifier is **core Lean 4, no Mathlib** — no `import`, no `norm_num/linarith/ring`.
- Path B **cannot win** with a banned strong closer (`omega`/`ring`/`decide`/full `simp`;
  config `prover_path_b_banned_tactics`). Prefer `rfl`-provable sub-goals so synthesis can
  legitimately win without retrieval.
- Each sub-goal is proved **independently** then stitched, so every `have` type must be
  self-contained and the closing lines must derive the target from the haves.
