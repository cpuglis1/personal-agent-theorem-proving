# Plan: Definition-mediated proving with cross-theorem-necessity acceptance

> Handoff doc for a fresh Claude Code window. Brainstormed + approved *direction*; the active
> research thrust and the **new baseline** for this work. The two-stage abstractor machinery
> (generalize→formalize, the bank, the weak-gate/necessity instruments) is **reused as substrate**,
> not discarded. Suggested branch: `concept-synthesis`.
>
> **Implementation status (live):**
> - **Phase 0 — soundness foundation: DONE.** The `sorryAx` gate below is implemented on the existing
>   sidecar (decision: stay on it now, Pantograph swap later). Sidecar `POST /axioms`,
>   `tools/lean_verify.lean_axioms`, `crews/soundness.py` (`axioms_clean` / `source_declares_gap` /
>   `soundness_ok`), config knob `prover_soundness_strict`. Offline suite green at 282 passed; the
>   `@pytest.mark.lean` live tier for `/axioms` needs a sidecar rebuild (`make lean-rebuild`).
> - **Phase 1 — proving primitive: DONE.** `prove_proposition(goal_type, seed_source, *, weak,
>   max_repair, decl, strict_soundness) -> ProofOutcome` is extracted from `verify_handler` Path B
>   (verify → bounded repair → weak gate → optional `soundness_ok` when `decl` given), which now calls
>   it with no behavior change (offline suite 290 passed; `tests/test_prove_proposition.py`). The
>   `decl` arg wires the Phase 0 soundness contract into the proving path — bridges/lemmas/ablation
>   re-proofs supply a decl and get `axioms_clean`; the verify node's anonymous `example` sources do
>   not. (Earlier the doc referenced `prove_proposition` as pre-existing substrate; it now exists.)
> - **Phases 2-4: not yet implemented.** Next: Phase 2 definition synthesis.

---

## Thesis

The system proves a target theorem the normal way, and **only when that stalls** it synthesizes a
*new local definition* (vocabulary) plus kernel-verified bridge lemmas, and proves the theorem
*through* that invented vocabulary. A synthesized definition is accepted as a genuine **concept**
only when it is **causally necessary across multiple theorems** — not merely helpful on the one
that triggered it.

**One line:** *definition-mediated proving with same-budget, cross-theorem causal acceptance.*

**Value-add, rooted in documented failure modes of the nearest systems:**
- **Minimo** (Poesia et al., NeurIPS 2024): its self-conjecturing loop drifts into *longer and
  more complicated* statements rather than *deeper* ones. **STP** (Dong & Ma 2025) bolts on
  hand-tuned "elegancy filters" to discard artificially-complicated conjectures. → self-improving
  provers collapse into **complexity, not fundamentality**.
- **LEGO-Prover case study** (2025): explicit library learning produced essentially **no real
  reuse** (≈1 name-reuse across 189 proofs; didn't survive to the final proof). → library growth
  ≠ compounding.
- **Aristotle** (informal → lemmas → formalize → repair → Monte-Carlo Graph Search over Lean
  sketches): every operation is *within the existing language* — lemma-gen adds facts in `L`,
  error-correction recovers a path in `L`. None of it **extends** `L`. So it raises the *floor*
  (fewer formalization failures) but cannot move the **findability ceiling** the vocabulary
  imposes.

Definition synthesis is the **only** operation that changes `L`. Definitions are conservative
(nothing newly provable in principle), but they reshape the search's depth structure: a proof
deep-and-unfindable in the standard vocabulary can become a short composition of bridge lemmas a
bounded prover actually finds. Marginal value should *grow with difficulty* — easy problems'
language is adequate (lemmas suffice); hard problems are disproportionately blocked by
expressiveness. This is the lever lemma-generation and error-correction structurally cannot pull.

**Do not overclaim** (writeup discipline): concept invention isn't new in spirit — Lenat's AM
(1976) invented "prime"; Colton's HR invented concepts in finite algebras. The fresh part is the
**modern, kernel-grounded, cross-theorem-necessity-certified** version. Claim *"definition-
mediated proving with same-budget causal acceptance,"* not *"first to invent concepts."*

---

## Soundness contract (NON-NEGOTIABLE) — the `sorryAx` gate, operationalized

Mirror Aristotle's standard exactly:

> While the system may use informal reasoning and code execution to find/draft, a problem is
> **solved** only if it produces a **complete** proof in Lean 4 + Mathlib, **without gaps or
> unsound axioms like `sorryAx`**.

**Mechanism — `soundness_ok(decl)`.** After *any* proof is produced, run `#print axioms <decl>`
through the interaction layer and parse the dependency list. **Implemented (Phase 0)** on the
existing Lean sidecar — `POST /axioms {source, decl}` → `tools/lean_verify.lean_axioms` →
`crews/soundness.soundness_ok` (not Pantograph; that swap is deferred). Lean prints one of:

```
'thm' depends on axioms: [propext, Classical.choice, Quot.sound]     ← PASS
'thm' does not depend on any axioms                                  ← PASS
'thm' depends on axioms: [sorryAx]                                   ← REJECT (gap)
'thm' depends on axioms: [..., Lean.ofReduceBool]                    ← REJECT in strict mode
```

`soundness_ok` returns **true iff** the parsed axiom set is a subset of the standard sound base
`{propext, Classical.choice, Quot.sound}`, with **no `sorryAx`** and **no user-declared axiom**.
Also reject any source containing `sorry`/`admit` or a new `axiom`. *(Strict mode, recommended
for headline runs: also reject `Lean.ofReduceBool` → disallows `native_decide`, whose trust rests
on the compiler rather than the kernel.)*

**Why this is also the completeness check.** In the draft-sketch-prove flow (see stack), an
unfilled hole is a `sorry`, and `sorry` elaborates to `sorryAx` — so `#print axioms` reporting
`sorryAx` *is* the signal that a hole was never closed. The same gate that rejects unsound axioms
therefore rejects incomplete proofs: **"soundness-clean" ≡ "every sketch hole closed."** One
check enforces both.

**Where it runs (every acceptance point):** each **bridge lemma**, each **planned lemma**, the
**parent theorem** — before that proof counts as solved or anything is banked. A concept is
bankable only if every bridge passes; a theorem counts solved only if its full proof passes.
Definitions are exempt from the *proof* check (no proof obligation) but must still **elaborate**
with no `sorry`.

---

## Definition vs theorem (implementation principle)

- A **definition** introduces *vocabulary* — an abbreviation, predicate, or structure
  (`def Balanced : List Nat → Prop := …`). No proof obligation, can't be true/false, only has to
  **elaborate**. It's a *conservative* extension — a bad definition can't make anything false,
  worst case vacuous/useless. **→ the LLM may invent freely here.**
- A **theorem / bridge lemma** is a *claim about* that vocabulary that must be **kernel-verified**
  (`theorem Balanced.step : Balanced xs → Balanced (f xs) := by …`). **→ all soundness lives
  here**, governed by the contract above.
- A **concept** = `(definition, [bridge lemmas])`. The definition is the noun; the bridges are
  the verified facts that make the noun usable.

---

## Workflow (escalation ladder)

Definition synthesis is **expensive and low-yield** (most invented definitions are crutches), so
it's an *escalation*, not the default. **Invent reactively** (only when stuck); **reuse
proactively** (the concept bank is consulted on every theorem's normal path).

```
1. NORMAL PATH (concept bank available)
     target → informal proof → lemma plan → formalize + repair
            → prove lemmas → prove theorem
     banked concepts are tried here automatically.
     solved (passes soundness contract) → DONE, nothing invented.

2. STALL DETECTION
     a planned lemma fails after r repair rounds AND B_normal is spent
     without closing the theorem → ESCALATE.
     the *stuck lemma* is the locus the definition is conditioned on
     (carries informal proof + lemma plan + formalized statements + Lean errors).

3. DEFINITION SYNTHESIS
     generate c candidate (definition + bridge lemmas), each aimed at the
     stuck lemma. cheap-gate each BEFORE proving (degeneracy gates).
     elaborate the definition; prove bridges within B_bridge; enforce
     the soundness contract on every bridge.

4. BIRTH ABLATION (same-budget causal test)
     re-prove the theorem THROUGH the package within B_ablate.
     accept provisionally iff:  solves-WITH (≤ B_ablate, soundness-clean)
                            AND  fails-WITHOUT (≤ B_ablate, identical budget).
     solves-without too → REJECT (caused nothing → crutch/redundant).

5. GIVE UP
     all c candidates fail → theorem UNSOLVED, move on. (Honest non-result.)

6. (outer, across the stream) PROMOTION / PRUNING
     promote a provisional concept to durable when it is causally necessary
     on ≥ k *later, distinct* theorems (same with/without test).
     prune a concept nothing reuses across the next m theorems.
```

Step 6 is what turns a trick (helped one proof) into a thesis (vocabulary **compounds**). Run
over a *stream* of theorems with the concept bank persisting.

---

## Recommended stack (local-feasible) — the MCGS replacement

Aristotle uses Monte-Carlo Graph Search over a 200B+ model — far too heavy here. The lightweight
equivalent that fits a single workstation/GPU:

**Lean interaction layer → Pantograph** (`lenianiva/PyPantograph`, TACAS 2025). Written in Lean
4; gives the agent control over policy/tactic functions; **forwards kernel errors**; runs
`#print axioms` directly (the soundness chokepoint); and crucially supports **`sorry`-extraction**
— feed it a sketch/definition with `sorry` holes and it returns each hole as a goal to fill. That
last feature *is* the draft-sketch-prove primitive below. Alternatives: `leanprover-community/repl`
(lower-level, simplest for just running `#print axioms`); `lean-lsp-mcp` (if you want
MCP/Claude-driven interaction); LeanDojo-v2 (heavier — requires repo tracing — but has built-in
whole-proof + search modes and retrieval).

**Search method → Draft-Sketch-Prove (DSP), not tree search.** Per goal/lemma: (a) a model emits
a *whole proof or a sketch with `sorry` holes*; (b) Pantograph extracts the holes as goals; (c)
close each hole with **hammers first** (`aesop`, `simp`, `omega`, `grind`, `linarith`,
`exact?`/`apply?`), and only fall back to the prover model for holes the hammers miss; (d) on
failure, feed the Lean error back and **repair** (`r` rounds). No MCGS infrastructure. This is the
same skeleton DeepSeek-Prover-V2 uses (generalist decomposes → small model fills subgoals) and is
what Pantograph was built to support.

**Models (the generalist/specialist split — the key local move):**
- **Informal reasoning, lemma planning, and definition synthesis** (the creative parts): a strong
  *generalist* — Claude or GPT via API, or a local 32B-class instruct model. This is the
  DeepSeek-V3-as-decomposer role; it doesn't need to be a prover.
- **Formal proving** (bridges, planned lemmas, the theorem): a small *specialist prover* run
  locally — **Goedel-Prover-V2-8B** is the top recommendation: an 8B model that matches the 671B
  DeepSeek-Prover-V2 on miniF2F (≈84.6% pass@32), runs on a single 16–24 GB GPU, and ships a
  **self-correction mode** (generate → Lean feedback → revise) that *is* your repair loop. Strong
  alternatives: **DeepSeek-Prover-V2-7B** (32K context, open) and **Kimina-Prover-Distill-8B**.

**Cheap closers / hammers** (always tried before the LLM, on every hole): `aesop` (white-box
best-first search, in Mathlib), `simp`, `omega`, `decide`, `grind`, `linarith`/`nlinarith`,
`exact?`/`apply?`. For your small axiomatic domains also consider **Canonical** (ITP 2025; closes
84% of the Natural Number Game) as a strong term-synthesis closer, and `lean-auto` to bridge to
external ATPs/hammers.

**Retrieval over the concept bank:** keep Qdrant for semantic recall, but prefer **symbolic
discrimination-tree retrieval** via Lean's own `exact?`/`apply?`/`simp` over banked concepts —
for formal premises the right matching criterion is *unifiability*, not embedding similarity.
(`LeanExplore` is an option for Mathlib-wide declaration search.)

---

## Budgets & knobs (defined, with defaults)

**All proving budgets are metered in two interchangeable units, whichever is hit first:**
(i) a **sampling budget** = number of *prover-model proof attempts (samples)* allowed, and
(ii) a **wall-clock cap**. Hammers are tried first on every hole and are **not** counted against
the sample budget (cheap/deterministic).

| Knob        | What it meters                                                              | Default            |
|-------------|----------------------------------------------------------------------------|--------------------|
| `B_normal`  | normal-path budget per theorem: generalist sketch samples `S` × prover work | `S=8` sketches, ≤300 s |
| `r`         | repair rounds per failing hole/lemma (= Lean-feedback self-correction)      | `3`                |
| `c`         | candidate (definition + bridges) generated when stuck                       | `4`                |
| `B_bridge`  | budget to prove one candidate's bridges (hammers + prover samples × `r`)    | ≤4 samples/bridge, ≤60 s |
| `B_ablate`  | the same-budget cap for the with/without ablation — **identical** both arms | **= `B_normal`**   |
| `k`         | distinct *later* theorems a concept must be necessary on to be promoted     | `2`                |
| `m`         | idle window (theorems seen) before pruning an unused concept                | `15`               |

`B_ablate` must be **exactly equal** across the with-package and without-package re-proofs — same
`S`, same `r`, same hammers — or "fails-without under the same budget" isn't apples-to-apples and
the causal claim collapses.

**Degeneracy gates** (cheap, pre-proving — kill bad definitions before spending `B_bridge`): no
`sorry`/`admit`/`axiom`; not `True`/`False`; doesn't mention the parent theorem's name; not
defeq-equivalent to the parent; `example : <def-unfolded goal> := by trivial` must **fail**
(non-vacuity); ≥1 bridge must verify soundness-clean before the package is ablated.

---

## Acceptance & promotion logic

- **Birth ablation** (provisional accept): `solves-with ∧ fails-without` at equal `B_ablate`,
  soundness-clean. `solves-without` → reject (crutch/redundant).
- **Promotion** (durable concept): causally necessary (same with/without test) on `≥ k` distinct
  *later* theorems.
- **Pruning:** discard a concept unused across the next `m` theorems.
- Anti-crutch is structural, not heuristic: a definition that's the hypothesis renamed passes
  neither birth ablation nor promotion.

---

## Architecture changes (wire into Hyperion)

The prover, bank, and necessity instruments already exist; this **re-roots** them.

**Reused as-is (repointed, nothing wasted):**
- Inner prover = existing `decompose → Path-A/Path-B`, invoked for steps 1, 3-bridge-proving, and
  the ablation re-proofs (now DSP-style over Pantograph + hammers + a local prover model).
- `prove_proposition(goal_type, hint_source=…, weak=…)` → proves bridges as **hinted synthesis**
  (hint = the informal sketch / stuck-lemma context).
- Bank (Qdrant `skill_library`) → now stores **concepts** `(definition, bridges, origin,
  times_won, necessity_hits)`.
- `generality_score` / `lemma_compare` → features for the degeneracy gates.
- Weak-gate + necessity counterfactual (`PROVER_WEAK_PATH_B`) → the engine behind **birth
  ablation** and **promotion** (the with/without, same-budget tests).
- `generalize` / anti-unification → optional structural backbone for the definition proposer
  (surface recurring structure across the lemma plan to *suggest* a definition; the LLM authors it).

**New handlers (`lean_handlers.py`) + agent records (`config/agents/`):**
- `synthesize_definition_handler` + `propose_definition(stuck_lemma, informal_proof, lemma_plan,
  formalized_lemmas, lean_errors)` → emits `c` candidates. New agent
  `definition_synthesizer.json` (generalist reasoner, strong alias, higher temp).
- `verify_concept_handler` → elaborate definition; prove/verify bridges via `prove_proposition`;
  enforce **`soundness_ok`** on every bridge; run degeneracy gates.
- `birth_ablation_handler` → the same-budget with/without test.
- `promotion` (stream-level, not a per-theorem node) → cross-theorem necessity + pruning.
- **`soundness_ok(decl)`** → wraps Pantograph `#print axioms`; the single chokepoint enforcing the
  `sorryAx` contract. Call it everywhere a proof is accepted/banked.

**DAG change (`config/workflows/lean-prove.json`):** add the escalation branch after repair/verify:
```
… formalize + repair → prove_lemmas → prove_theorem
                          │  (stall: r repairs exhausted, B_normal spent)
                          └──→ synthesize_definition → verify_concept
                                 → (prove lemmas/theorem through vocabulary)
                                 → birth_ablation
                                     ├─ accept → bank (provisional)
                                     └─ reject/exhaust c → unsolved
```
Promotion/pruning runs at the **stream driver** level (over `tasks/*`), not inside one run.

**Other files:** `eval/trace.py` — stage labels (`synthesize_definition`, `verify_concept`,
`birth_ablation`) + fields (`escalated`, `concept_id`, `birth_ablation_pass`, `axioms_clean`,
`necessity_hits`). `tests/` — see Verification.

---

## Evaluation (how the thesis is determined)

An **existence-and-rate** loop, not a pass-rate competition.

- **Dependent variable:** *certified reusable invented concepts* — count, rate, budget. Certified
  by **cross-theorem necessity** (promotion), not by helping one proof.
- **Keep the eval off the steering signal.** "Helped the triggering theorem" is the birth signal;
  certification is the *independent* multi-theorem necessity test. Do not let promotion stats feed
  back into synthesis/selection — grading on your own yardstick voids the result.
- **Anti-crutch is structural:** birth ablation requires *fails-without*; promotion requires
  necessity on `≥k` distinct theorems.
- *(Optional, strongest)* **extrinsic transfer:** does the grown bank help prove *held-out human
  theorems* (Natural Number Game, a textbook section) under fixed budget? Deep concepts transfer;
  crutches don't.
- **Domains:** small axiomatic, Minimo-comparable, laptop-scale — propositional logic, arithmetic,
  group theory. *Arithmetic* = pathology showcase (complexity-collapse worst). *Group theory* =
  depth showcase (real structure — inverses, conjugation, homomorphism-flavored predicates).

**What this loop can reasonably do.** On a curated *stream* of related theorems in a bounded
domain: solve most theorems on the normal path; invent definitions for a minority of stuck ones;
and — the rare valuable event — produce a *handful* that survive cross-theorem necessity, i.e.
genuine reusable concepts. Most invented definitions will be crutches and get pruned; the cross-
theorem filter exists to separate the few real concepts from the many crutches. It will **not**
invent deep concepts on demand or at competition scale. A single clean case — a definition the
system invented (not in the prompt) that several later proofs **needed** — is the striking result.

---

## Thesis positioning (writeup framing, not code)

- **Closest:** Aristotle (within-language: lemma-gen + repair + MCGS — no language extension);
  Minimo / STP (conjecture *theorems*, complexity-not-depth collapse); LeanAgent (accumulates
  *existing* premises + trains the retriever — doesn't *invent* definitions); LEGO-Prover
  (reuse-null).
- **Methodological ancestor:** DreamCoder → Stitch → **babble** (anti-unification library learning
  "modulo an equational theory" ≈ abstracting proofs modulo defeq/simp). Cite it. Classical
  concept formation: AM, HR.
- **Distinctive here:** definition synthesis as the *language-extending* operation, gated by a
  **same-budget causal birth test** and **cross-theorem necessity** promotion, with the `sorryAx`
  soundness contract throughout. Framing: a *measurement + mechanism* thesis on a small explicit
  system, not a SOTA-prover claim.

---

## Verification

1. **Offline** (`pytest tests -q -m 'not lean'`, keep green): unit tests cover —
   - `soundness_ok`: rejects a `sorry`/`sorryAx` proof; rejects a user `axiom`; (strict) rejects
     `native_decide`/`Lean.ofReduceBool`; **accepts** a proof whose `#print axioms` ⊆
     `{propext, Classical.choice, Quot.sound}`; parses the "does not depend on any axioms" case.
   - degeneracy gates: reject `True`/`False`, parent-shaped, parent-name-mentioning, vacuous
     definitions.
   - birth ablation: accept on `solves-with ∧ fails-without`; reject on `solves-without`; assert
     `B_ablate` is identical across arms.
   - promotion/pruning over a mocked stream (necessary on `≥k` → durable; idle `m` → pruned).
2. **Live smoke (the real test):** Pantograph + a local prover (Goedel-Prover-V2-8B) + hammers.
   Reset bank; run a **small, related theorem stream** in group theory designed so one definition
   (e.g. a `Commutator`/`Conjugate`-style predicate) should emerge on a stuck theorem and then be
   *reused* by ≥2 later ones. **Success =** the bank holds a concept whose definition was
   synthesized (not seeded), every bridge passes the soundness contract (`axioms_clean=true`, no
   `sorryAx`), and `necessity_hits ≥ k`. Inspect `tasks/<id>/context.json` for
   `synthesize_definition`, `verify_concept`, `birth_ablation_pass`.

---

## Out of scope (follow-ons)

- Full RL self-play training of the conjecturer/prover (this studies the *signal/mechanism* with a
  fixed-or-light prover; full RL is a scale-up).
- **Depth-steering** as an explicit conjecturer objective (anti-unification novelty vs the bank) —
  sibling thesis; could later prioritize *which* stuck theorems to attempt or *which* candidate
  definitions to try first.
- **Compression / MDL** of the concept bank as an additional concept-quality measure.
- Extrinsic-transfer evaluation harness against held-out human theorems.
- RAG over an informal math corpus at the definition-proposal step.
- Migrating Path-B `verify_handler` onto `prove_proposition` (Path B stays the steady-state fallback).
