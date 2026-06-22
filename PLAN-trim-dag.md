# Plan: Trim lean-prove to a verified agentic pipeline + one simple thesis

> Handoff for a Claude Code window in the Hyperion repo. **Supersedes all prior drafts.**
> The deliverable is **the pipeline** — a well-built agentic system that drives an LLM to produce
> kernel-verified Lean — which also answers **one simple question** (below). No causal-acceptance
> apparatus, no cross-theorem necessity, no regimes. Keyed to `WORKFLOW-AND-THESIS.md` snapshot
> 2026-06-22. Suggested branch: `trim-to-pipeline`.

## Implementation status

As of commit `f4b67fa` (`trim lean prover DAG`), the cut pass is implemented in Hyperion:
`compare`, `abstract`, `birth_ablation`, weak/strong regimes, and cross-theorem
promotion/pruning are removed from the runtime and tests. The active concept branch is
`escalation_gate → synthesize_definition → verify_concept → prove_through → bank_concept`.

## The one thesis (kept simple)

> **When the basic pipeline fails to close a goal, does LLM-proposed definition synthesis
> (kernel-verified) let it close goals it otherwise couldn't?**

That's the whole claim. "Improve results" is measured as a **before/after**, not a per-goal causal
test (see §Measurement).

## The one principle

**The LLM proposes, the kernel disposes.** Everything generative is untrusted; exactly one
component — the Lean kernel — is the source of truth. This applies to synthesized definitions too:
a proposed `def` is worthless until its bridge lemmas are kernel-verified and pass the soundness
gate. Nothing is accepted because the model said so.

## Target DAG

```
intake ─▶ decompose ─▶ skeleton_check ─▶ [ battery → synth → repair ] ─▶ verify ─▶ assemble ─▶ final_verify
 (goal      (LLM:        (kernel,           per sub-goal:                 (kernel,    (stitch     (kernel,
  in)        plan the     fail-fast)         cheap → LLM → fix-loop)       full mode)  into        whole proof,
             have-chain)                                                               scaffold)    no sorry)
                                                  │ (basic path stalled: goal did not close)
                                                  └─▶ escalation_gate ─▶ synthesize_definition ─▶ verify_concept ─▶ prove_through ─▶ assemble ─▶ final_verify
                                                       (route stall)      (LLM: def + bridges)    (kernel: def       (re-prove the goal
                                                                                                   elaborates,        with the verified
                                                                                                   bridges proven,    def + bridges in
                                                                                                   soundness-clean)   scope)
```

Per-sub-goal fan-out stays: scaffolds with >1 `have` clone the `[battery→synth→repair]→verify`
segment per sub-goal; `assemble` fans in.

## Critical pieces — the pipeline (all load-bearing engineering; keep)

1. **Warm Lean verifier (sidecar).** Long-lived Lean + Mathlib preloaded; first call ~24s, every
   verify after sub-second. State isolation (branch from BASE_ENV, discard returned env id).
2. **Decompose → skeleton_check (fail-fast planning).** LLM proposes a `have`-chain scaffold; kernel
   type-checks the *shape* with `sorry` allowed before any proving budget; bad plan ⇒ bounded
   revise (≤2).
3. **Tiered per-sub-goal proving (battery → synth → repair).**
   - **battery** (deterministic, no LLM, first): `rfl, simp, omega, decide, norm_num, ring, aesop, …`
     through the kernel; runner **skips synth** when the battery closes the goal (the cost control —
     the 226× collapse).
   - **synth** (LLM): only when the battery misses.
   - **repair** (bounded LLM): kernel error fed back, ≤`r` retries. The agentic act→observe→revise loop.
4. **verify = kernel, full mode.** Every candidate, every tier, no `sorry`. Sole arbiter.
5. **assemble → final_verify.** Stitch verified sub-proofs into the scaffold, write `result.lean`,
   verify the **whole** proof. "Each piece passed" ≠ "the composition is valid."
6. **Soundness gate (`#print axioms`).** Cheap guard: no `sorryAx` (skipped hole), no sketchy axiom.
   Applies to bridge lemmas too. "Verify the verifier wasn't gamed."
7. **Observability / trace.** Structured events per stage — plan, which tier closed each sub-goal,
   kernel verdicts, costs, timings, and whether escalation fired + what def it produced. Keep it.

## Critical pieces — the thesis branch (keep, kernel-verified)

8. **escalation_gate** — routes a goal the basic path **failed** to close into definition synthesis.
   "Basic path failed" = the full pipeline (decompose + battery + synth + repair) genuinely couldn't
   close it. No artificial weakening.
9. **synthesize_definition** — LLM proposes a new `def` + bridge lemmas aimed at the stalled goal.
   Cheap degeneracy filters before proving (not `True`/`False`, not the goal renamed, etc.).
10. **verify_concept** — the def must elaborate; every bridge lemma must be **kernel-proved and
    soundness-clean**. Only then is the def a usable concept. This is the verification spine applied
    to the new branch — not optional.
11. **prove_through** — re-attempt the stalled goal with the verified def + bridges in scope, using
    the same prover (battery/synth/repair). If it now closes → assemble → final_verify. If not → that
    goal is honestly unsolved.

## Measurement (the before/after — this replaces birth ablation)

Run the eval set **twice at the same proving budget**:

- **Escalation OFF** (baseline): the basic pipeline only.
- **Escalation ON**: basic pipeline + the §thesis-branch.

The goals that close **only with escalation ON**, proved *through* a synthesized def whose bridges
are kernel-verified, are the result. The OFF run is the "without" condition — so no per-goal
same-budget counterfactual is needed.

**Honest budget note (one line, keep it):** hold the proving budget equal across OFF/ON so "improved
results" can't be "I gave it more attempts."

**Eval-set caveat:** there must be goals the basic path *can't* close, or there's nothing for
definition synthesis to rescue. Full-strength arithmetic mostly closes via the battery, so the
interesting candidates are structurally deeper goals (group-theory-flavored) where the missing piece
is a *predicate/abbreviation*, not a missing tactic.

## What to CUT

- **birth_ablation** — the per-goal same-budget WITH/WITHOUT causal test. **Replaced** by the OFF/ON
  before/after above. Remove the node and `birth_ablation_handler`.
- **Cross-theorem promotion + pruning** — stream driver, `concept_promote_k` / `concept_prune_idle_m`,
  `necessity_hits`. (No "compounds across theorems" claim.)
- **Weak/strong regime** — `prover_weak_path_b`, `_uses_only_weak_tactics`, the `b_strong`
  counterfactual. The battery wins with whatever closes the goal; "basic path failed" is defined by
  real failure, not a weakened prover.
- **`compare` A-vs-B scoring (T5)** — `triple_log`, reuse/generalization/shortness scoring. If a
  cache hit and a synth candidate both verify, trivial pick; otherwise the node disappears.
- **`abstract`** — generalization-before-banking. Remove from the chain.

## Optional (carries no weight; include or not)

- **Lemma / concept cache.** Memoize proved lemmas and accepted concepts; consult before synth on
  later goals. This is what the old Path-A "retrieve" + banking becomes if kept — a cache consult
  before synth, a cache write after `final_verify`. Off by default for the simple thesis.

## Trace fields after the cut

Keep: plan/scaffold, per-sub-goal `tier_closed` (battery/synth/repair) + `winner`, kernel verdict,
`escalated`, `concept_id` (what def synth produced), `axioms_clean`, cost, timing, `final_verify`.
Drop: `birth_ablation_pass`, `b_strong_closed`, `necessity_hits`, `triple_log`.

## Profiles

Keep the sidecar's `core` (rejects `import`) and `mathlib` (full battery needs it) profiles; default
the demo to `mathlib`.

## Build order

1. **Fix the ∀-threaded assembly bug first** (`lean_handlers`: `_threaded_goal_type` / `_assemble`
   / `_proof_body_for_hole`) — lives in `assemble`/`final_verify`; gates the multi-sub-goal path.
2. **Do the cuts** (birth_ablation, regimes, promotion, compare scoring, abstract); keep the suite
   green at each step.
3. **Simplify the thesis branch** to `escalation_gate → synthesize_definition → verify_concept →
   prove_through → assemble → final_verify` (no birth_ablation).
4. **Build an eval set with genuinely-hard goals** (basic path fails on some).
5. **Run OFF vs ON** at equal budget; the delta is the result.

## Tests / hygiene

- **Keep:** verifier smoke/state-isolation, skeleton fail-fast + bounded revise, battery-first +
  skip-synth, repair loop, fan-out e2e, `assemble`/`final_verify`, `soundness_ok`, def
  elaborates + bridges kernel-proved (`verify_concept`).
- **Remove:** birth-ablation, promotion/pruning, weak-gate, `compare` scoring tests.
- **Hygiene:** kernel is the only judge of every attempt (including bridges); no interim verifier
  bandaids; `intake`/`formal_ingest` stays deterministic, no-LLM; hold budget equal across OFF/ON.

## Why this is the portfolio story

The pipeline shows the RE-relevant skills directly: verifier-in-the-loop (≈ RL-from-verifiable-reward
/ agentic tool use), tiered battery→synth (cost engineering), the repair loop (agentic feedback),
and the trace (observability/eval discipline). The one thesis adds that you can **pose a clean
question and answer it with an honest before/after** — without overbuilding. Lean is the concrete,
impressive instance.

## Out of scope

- Pantograph + DSP migration (separate build-forward).
- Anything reintroducing causal birth ablation, regimes, promotion/pruning, or cross-theorem claims.
