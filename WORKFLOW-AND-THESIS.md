# lean-prove — Current Workflow & Thesis Under Test

_Snapshot: 2026-06-22, branch `postwork-eval-observability`._

This document is the single-page map of (1) what the `lean-prove` workflow currently
**does**, and (2) which **thesis claims** each part of it is meant to test, with the
**current measured status** of each. It is deliberately separated from
`LEAN-PROVE-PIPELINE.md` (which is the per-node mechanical spec) — this one is about
*what we are claiming and whether the evidence is in yet*.

---

## 0. One-sentence framing

> `lean-prove` is two contracts glued together: **an LLM proposes structure or proof
> text, and the Lean kernel decides whether that text is real.** The LLM can suggest; it
> never gets to certify.

Every "win" in this system is a kernel verdict (`/verify` full mode, no `sorry`) plus a
soundness check (`#print axioms`, no `sorryAx`). Nothing downstream of the kernel can
manufacture a green.

---

## 1. The workflow (shipped single chain)

Source of truth: `agents/hyperion/config/workflows/lean-prove.json`.

```
decompose ─▶ skeleton_check ─▶ ┌ retrieve ┐                ┌ compare ─▶ abstract ─┐
            (type-check the   │  (Path A) ├─▶ verify ─▶────┤                      ├─▶ bank
             have-chain in    └ synthesize┘  (native       └ escalation_gate ─▶ … ─┘
             skeleton mode)     (Path B)      controller)     synthesize_definition
                                                              verify_concept
                                                              birth_ablation
                                                              bank_concept
```

The blackboard is **sub-goal-namespaced** (`candidate_b:<sg>`, `discharged:<sg>`, …).
After `skeleton_check` passes with >1 active sub-goal, the runner clones every node
between `skeleton_check` and `bank` once per sub-goal (`<node>__<sg>`); `bank` fans in
over all clones and only succeeds if **every** sub-goal is discharged.

### Stage roles (who decides what)

| Stage | LLM? | What it produces | Who certifies |
|---|---|---|---|
| **decompose** | yes (`decomposer`, tool-less, 1 call) | `plan.md`: a `have h : T := sorry` scaffold + a self-describing `closer` tactic + per-sub-goal `lean_type` | nothing here (pure generation) |
| **skeleton_check** | no | does the have-chain *shape* compose to the target? | Lean kernel, `skeleton` mode (`sorry` allowed) |
| **retrieve (Path A)** | no | candidate proofs built from banked lemmas (re-prove as `have h; first \| exact h \| apply h \| simpa using h` to instantiate ∀-binders) | proposes only |
| **synthesize (Path B)** | yes (`lemma_synthesizer`, tool-less, 1 call, runner-captured JSON) | a self-contained Lean proof for one sub-goal | proposes only |
| **verify** | controller native; repair LLM | runs the **closer battery first** (kernel, $0), then the LLM seed + bounded repair only if needed; applies the weak gate | Lean kernel, `full` mode |
| **compare** | no | deterministic A-vs-B winner (reuse / generalization / shortness); writes the thesis triple-log | compares already-verified candidates only |
| **escalation_gate** | no | routes a stalled sub-goal into definition synthesis | branch routing only |
| **synthesize_definition** | yes (`definition_synthesizer`) | a new `def` + bridge lemmas, with cheap degeneracy filters | static checks here |
| **verify_concept** | repair LLM | definition elaborates, bridges prove, soundness contract holds | Lean kernel + `#print axioms` |
| **birth_ablation** | repair LLM | re-prove target WITH vs WITHOUT the new concept, same budget | Lean kernel; accept only if WITH succeeds **and** WITHOUT fails (causal) |
| **bank_concept / bank** | no | assemble sorry-free `result.lean`, **full-verify it**, bank winning lemmas/concepts | Lean kernel, `full` mode — final ground truth |

### Verifier substrate (load-bearing, recently rebuilt)

- **Warm Mathlib REPL sidecar** (`agents/hyperion/lean-sidecar/{server.py,Dockerfile}`):
  a long-lived `leanprover-community/repl` (rev `2196679` = Lean v4.15.0) with a hot
  `import Mathlib` BASE_ENV. First call ~24s one-time load; subsequent verifies are
  **sub-second (~0.05s)**. Every `/verify` and `/axioms` branches from BASE_ENV and
  **discards** the returned env id (state isolation — verified).
- Two profiles: `core` (rejects any `import`; pure Lean 4) and `mathlib` (the dev rows
  all run `mathlib`).

---

## 2. The closer battery — a **control condition**, not the result

Before the LLM seed runs, `verify` tries a deterministic battery of standard one-shot
closers through the kernel (`_run_closer_battery` in `crews/lean_handlers.py`):

```
rfl, simp only [], decide, norm_num, ring, ring_nf, omega, simp, linarith, nlinarith, positivity, aesop
```

(∀/→ goals also get an `intros; <closer>` variant.) **Win-eligibility is not encoded in
the battery** — it is decided downstream by the *same* `_uses_only_weak_tactics` gate
Path B already uses (one enforcement path, never a hand-labelled flag). This is the crux
of the experiment and is governed by the prover **regime** (§3).

The battery also runs **first** (battery-first) and the runner skips the synth LLM when
the battery would win-eligibly close the goal — both a cost control (see the 226× cost
collapse on algebra-462) and a way to keep trivial closes from masquerading as composed
proofs (`origin="battery"` tags them so the thesis read-out can separate them out).

---

## 3. The thesis portions under test

The whole point is **two regimes, two numbers**. The battery's win-eligible contents are
a *function of the regime*:

| Regime | `prover_weak_path_b` | Battery may WIN with | Reported as |
|---|---|---|---|
| **Strong** | `False` (default) | the full battery (incl. `norm_num`/`ring`/`decide`/`omega`/`simp`/…) | external / **SOTA-comparison** number |
| **Weak** | `True` | **only primitives** (`rfl`, structural `intros`, narrow `simp only`). Strong closers are still **TRIED** and recorded as the `b_strong` counterfactual, but **BANNED FROM WINNING**. | **thesis** number |

The weak regime is the thesis-relevant one **because it forces composition and
definition-mediation to carry the non-trivial goals**. If a goal can only be closed by a
strong one-liner, the weak prover is not allowed to win that way — it must instead
decompose the goal and stitch sub-results, or invent a new definition + bridge lemmas and
prove *those*. That is the capability we are actually claiming.

### Claim → mechanism → status

| # | Thesis claim | Mechanism in the workflow | Status |
|---|---|---|---|
| **T1** | Decomposition + a kernel-checked skeleton reliably reduces a goal to self-contained sub-goals. | `decompose` (self-describing `closer`) → `skeleton_check` → per-sub-goal fan-out, with an **identity-decomposition floor** on budget exhaustion. | **SOLVED.** All 3 dev rows pass skeleton and reach the prover; `subgoal_unbound_context` empty on every row. |
| **T2** | A weak prover (primitives only) forces non-trivial goals onto **composition** instead of a strong one-liner. | Regime-gated battery + `_uses_only_weak_tactics` gate; `b_strong` counterfactual recorded. | **Control validated.** In weak mode, algebra-462 & algebra-182 correctly fall through (their `norm_num`/`ring` closers are gated out); numbertheory-132 closes via primitive `rfl`. |
| **T3** | Banked lemmas are **retrieved and reused** to discharge later *instance* goals (the "snowball"). | Path A: `_candidate_from_lemma` re-proves a banked ∀-lemma locally and `apply`/`simpa`s it; retrieval query normalized to the bare goal type. | **Demonstrated in isolation** (goal `0 + 9 = 9` retrieves `nat_zero_add` and wins on Path A, `winner_path: A`). **Not yet shown end-to-end inside the dev battery run.** |
| **T4** | When normal proof stalls, the system **invents new vocabulary** (a `def` + bridge lemmas) and only keeps it if it is **causally** necessary. | `escalation_gate` → `synthesize_definition` → `verify_concept` → `birth_ablation` (WITH-vs-WITHOUT, same budget) → `bank_concept`. | **Wired, not yet exercised on a real dev stall.** This is the next machinery the weak fall-through goals (462, 182) are supposed to drive. |
| **T5** | A vs B is **measured**, not assumed — reuse/generalization is scored deterministically. | `compare` writes `triple_log:<sg>` (scores, winner path) over already-verified A/B candidates. | **Instrumented.** Trace exposes per-sub-goal candidates, verdict, discharge path, and `b_strong_closed`. |

---

## 4. Current dev baseline (the live numbers)

**Eval set:** `agents/hyperion/evals/lean_prove_splits/dev.jsonl` — 3 public miniF2F-valid
rows, all `lean_profile: mathlib`:

| case_id | goal |
|---|---|
| `mathd_algebra_182` | `7 * (3*y + 2) = 21*y + 14` (with `y : ℂ`) |
| `mathd_algebra_462` | `((1:ℚ)/2 + 1/3) * (1/2 - 1/3) = 5/36` |
| `mathd_numbertheory_132` | `2004 % 12 = 0` |

**Run 2026-06-22, both regimes (research on, `PROVER_WEAK_PATH_B` toggled server-side):**

| Regime | Result | Detail |
|---|---|---|
| **WEAK** (thesis headline) | **1/3** | 132 closes via battery `rfl` (a primitive — honestly trivial). 462 & 182 **correctly fall through** — their closers are gated out, so they must be carried by composition/definition (the control holds exactly as designed). |
| **STRONG** (SOTA-comparison) | **2/3** | 462 ✓ (battery `norm_num`), 132 ✓ (battery `rfl`). **182 ✗.** |

**Reading the numbers honestly:** the weak `1/3` is *not yet* a thesis win — 132 is a
trivial primitive close, and the two goals that would actually demonstrate the thesis
(462, 182, carried by composition/definition) are not closed yet. The thesis claim lives
in **composition/definition-mediated wins in the weak regime**, of which there are
currently **zero**. That is the open frontier, not the headline.

### Known blocker (not the thesis machinery)

**182 fails in `bank`, not in proving.** Both its sub-goals are `discharged via Path B`
($0, 0 LLM calls), but final assembly emits

```lean
have h1 : … := by exact (by intros; ring) y
```

— applying a tactic-block term to the bound `y : ℂ` with no expected type, which Lean
rejects (`invalid 'by' tactic, expected type has not been provided`). The **∀-threaded
multi-sub-goal assembly** that re-applies a threaded ∀-proof to the parent's bound
variable is malformed. Fix lives in `lean_handlers` (`_threaded_goal_type` +
`_assemble` / `_proof_body_for_hole`). 462/132 are unaffected (no free var / single goal).

---

## 5. Experimental hygiene (guardrails)

- **Never run the frozen `test` split** (1 row) — running it destroys its holdout value.
  Only `smoke` (wiring), `train` (learning writes), `dev` (this baseline).
- **No interim bandaids** in the verifier (no timeout/import workarounds) — the warm REPL
  is the durable fix.
- **`formal_ingest` stays deterministic / no-LLM** — it only structurally splits the
  given statement; it must never infer goals, rewrite the statement, or retrieve the
  target, or the SOTA comparison is tainted.
- **Win-eligibility only via `_uses_only_weak_tactics`** — never a hand-labelled "this is
  a weak tactic."
- **The kernel is the only judge** of every battery / seed / repair attempt; a tactic
  that doesn't close is skipped, nothing is faked.
- In `dev`, episode/lemma writes are **skipped** (`eval_mode=dev`), so battery wins can't
  pollute the bank. For `train`, skip `abstract`/`bank` for `origin == "battery"`.

---

## 6. What "done" looks like for the thesis (open frontier)

The next results that would actually move the thesis from *control-validated* to
*demonstrated*:

1. **Fix the ∀-threaded assembly bug** so 182 banks → unblocks the first composed proof.
2. **A weak-regime composition win**: 462 or 182 closed by stitching sub-results whose
   individual closers are weak-eligible (not by a gated-out one-liner). This is the first
   real T2 datapoint.
3. **A weak-regime definition-mediated win** (T4): a dev goal that stalls under primitives
   drives `synthesize_definition` → `birth_ablation` accepts a causally-necessary concept.
4. **An end-to-end snowball inside the dev run** (T3): a lemma banked on one row retrieved
   and reused via Path A on a later row.

Until (2)–(4) land, the honest summary is: **the verifier, decomposition, and the
regime-gated control are solid; the thesis-relevant wins (composition- and
definition-mediated proof in the weak regime) have not yet been produced.**
