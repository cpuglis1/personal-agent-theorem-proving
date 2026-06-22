# Handoff: regime-gated deterministic closer battery (Path-B seed)

You are picking up the `lean-prove` theorem-proving workflow. The verifier infra and the
decomposition/skeleton stage are now solved. This handoff is to add a **deterministic closer
battery** as the Path-B seed — the thing that actually closes sub-goals — implemented as a
**control condition that is a function of the prover regime**, not a blanket cure.

**Do NOT run the `test` split** (frozen holdout). Only `dev` (this baseline) and `train`.
**Do NOT trivialize the thesis**: the battery must obey the weak gate (see §"The whole point").

---

## Context already done (do not redo)

1. **Warm Mathlib REPL sidecar** — `agents/hyperion/lean-sidecar/{server.py,Dockerfile}` now
   drives a long-lived `leanprover-community/repl` (pinned rev `2196679` = Lean v4.15.0) with a
   hot `import Mathlib` BASE_ENV. Sub-second verifies; state-isolated; `/verify`,`/axioms`,
   `/health` contract preserved. Validated (good/bad/sorry/axioms/isolation). See memory
   `warm-repl-verifier-and-dev-baseline`.
2. **Decomposition/skeleton SOLVED** via two changes (both live-verified):
   - **(B) self-describing closer**: `PlanOption.closer` carries the composing tactic;
     `skeleton_check` kernel-checks `have …; <closer>`. The old `_canonicalize_closing`
     bandaid was removed. Files: `crews/plan_contract.py` (`closer`, `active_closer()`),
     `crews/lean_handlers.py` (`_native_closing_for_subtasks`/`_native_scaffold_from_subtasks`
     thread the closer; sanitizer no longer rewrites closers), `crews/runner.py`
     (decomposer prompt emits `closer`).
   - **(A) identity-decomposition floor**: on skeleton revision-budget exhaustion the runner
     writes the always-valid identity plan (`have h1 : G := sorry; exact h1`, closer
     `exact h1`) via `lean_handlers.write_identity_plan` and re-enters (`floor_applied` guards
     one shot) instead of `_failed`.
   Tests updated/added in `tests/test_lean_prove_workflow.py` and
   `tests/test_plan_contract_lean.py`; all green (`pytest -m "not lean"` + `-m lean`).

**Result of the last dev rerun: still 0/3, but all 3 now get PAST skeleton into the prover.**
The uniform failure moved to `cannot assemble result.lean; undischarged sub-goal(s)` — the
sub-goal prover can't close them. That is what this handoff fixes.

---

## The diagnosis (why the prover can't close trivial goals)

Cold-bank run, so Path A (retrieval) is empty (0 lemmas — expected). The ONLY thing that
generates a proof attempt is the Path-B **`lemma_synthesizer` LLM seed**, and its output is
consistently bad. Actual seeds pulled from the run blackboard (`ai-router/tasks/<id>/context.json`,
key `candidate_b:<sg>`):

- **462** `((1:ℚ)/2+1/3)*(1/2-1/3)=5/36`: emitted **Lean 3 syntax** — `calc … : by …` with `...`
  continuations, hallucinated `Rat.mul_div`.
- **132 h1** `2004/12*12=2004`: Lean 3 `by { t1, t2 }` comma-block.
- **182 h1** `7*(3*y+2)=21*y+14`: `simp only [...]; rfl` (rfl can't finish; needs `ring`).

`propose_repair` (one tool-less LLM call/round, same model family, `cap_repair_iters=3`) can't
rescue Lean-3-biased seeds: every failed sub-goal shows `B-repair ok:false ×3`.

**There is NO deterministic tactic attempt anywhere in the prover path** (confirmed by grep).
Yet every failed sub-goal closes with ONE standard tactic — verified live against the sidecar:

| sub-goal | one tactic | verdict |
|---|---|---|
| 462 h1 | `by norm_num` | ✅ |
| 182 h1 (∀-threaded) | `by intros; ring` | ✅ |
| 132 h1 | `by norm_num` / `by rfl` | ✅ |
| 132 h2 | `by decide` / `by norm_num` / `by rfl` | ✅ |

The synthesizer tried none of them. A deterministic battery of standard closers, tried via the
kernel before the LLM, closes all of these.

---

## The whole point (thesis line — DO NOT cross it)

Two regimes, two numbers, and **the battery's win-eligible contents are a function of the regime**:

- **Strong** (`settings.prover_weak_path_b = False`, current default): the full battery may
  WIN. This is the external/SOTA-comparison number. Fine to report — but it is NOT the thesis.
- **Weak** (`settings.prover_weak_path_b = True`): only **primitives** may win (`rfl`,
  structural `intros`, narrow `simp only`). Strong closers (`norm_num`/`decide`/`ring`/`omega`/
  `simp`/`linarith`/`aesop`/…) are still **TRIED** but **BANNED FROM WINNING** — recorded as the
  `b_strong` counterfactual. This is the thesis-relevant number, because it forces **composition
  and definition-mediation** to carry the non-trivial goals. The battery here is the **control
  condition**, not the cure.

Enforce win-eligibility through the **existing** `_uses_only_weak_tactics` gate
(`crews/lean_handlers.py`), the SAME one Path B already uses — one mechanical enforcement path,
never a hand-labelled flag. Strong closers appear in `settings.prover_path_b_banned_tactics`
= `('omega','decide','native_decide','ring','ring_nf','linarith','nlinarith','polyrith',
'norm_num','aesop','tauto','simp_all','field_simp')`; the gate also bans bare `simp` (but allows
`simp only`).

**Validated control behavior** (live, against the sidecar): in weak mode `rfl`/`simp only []`
FAIL on 462 (forces composition) but `rfl` CLOSES 132 h1/h2 by kernel computation (132 is
honestly trivial even for the weak prover). So expected splits:
- Strong: battery closes 182/462/132 → 3/3 (SOTA number).
- Weak: battery closes 132 (via `rfl`); 462 and 182 h1 fall through to composition/definition
  (the thesis machinery must earn those).

Do not let the battery be both the base and the headline: report the strong number as
SOTA-comparison and the weak number as the thesis result, clearly labelled.

---

## Implementation plan

All edits in `agents/hyperion/src/hyperion/crews/lean_handlers.py` unless noted.

### 1. Battery helpers (insert just before `async def verify_handler`, ~line 1146)

Reuses `_full_verdict(src, profile=...)` (~959), `_uses_only_weak_tactics(src)` (~972),
`_bare_proof_term({"source": src})` (used at ~1121/1245), `settings`.

```python
# Standard one-shot closers, cheapest/most-primitive first. Win-eligibility is NOT encoded
# here — it is decided downstream by the SAME _uses_only_weak_tactics gate Path B uses (one
# enforcement path). Consequence (the thesis contract):
#   • strong regime: every closer may WIN  → external/SOTA number.
#   • weak regime  : only primitives (rfl / structural intros / narrow `simp only`) may win;
#     the strong closers are still TRIED and recorded as the b_strong counterfactual but are
#     BANNED FROM WINNING — the control condition that forces composition + definition-mediation.
_BATTERY_CLOSERS = (
    "rfl",            # primitive: kernel computation / definitional
    "simp only []",   # primitive: narrow simp (weak-eligible)
    "decide", "norm_num", "ring", "ring_nf", "omega",
    "simp", "linarith", "nlinarith", "positivity", "aesop",
)

def _battery_source(goal_type: str, tactic: str, profile: str | None) -> str:
    head = "import Mathlib\n\n" if (profile or settings.lean_profile or "").strip().lower() == "mathlib" else ""
    return f"{head}example : {goal_type} := by {tactic}"

def _closer_battery_tactics(goal_type: str) -> list[str]:
    # ∀/→ goals also get a structural-`intros` prefix so a closer fires after binders are
    # introduced. `intros` is a no-op (not an error) on a non-arrow goal — verified live — so
    # the prefixed variant is always safe to try.
    needs_intro = goal_type.strip().startswith("∀") or "→" in goal_type
    out: list[str] = []
    for c in _BATTERY_CLOSERS:
        out.append(c)
        if needs_intro:
            out.append(f"intros; {c}")
    return out

def _run_closer_battery(goal_type, *, weak, profile):
    """Returns (weak_source, strong_source, verdicts). strong_source = first close at full
    strength (b_strong counterfactual / the strong-regime win); weak_source = first close that
    ALSO passes the weak gate. Strong regime ⇒ weak_source == strong_source. Kernel is the only
    judge; a tactic that doesn't close is skipped."""
    strong_source = weak_source = None
    verdicts = []
    for tac in _closer_battery_tactics(goal_type):
        src = _battery_source(goal_type, tac, profile)
        closed, _ = _full_verdict(src, profile=profile)
        verdicts.append({"tactic": tac, "ok": closed})
        if not closed:
            continue
        if strong_source is None:
            strong_source = src
        if not weak or _uses_only_weak_tactics(src):
            weak_source = src
            break
    return weak_source, strong_source, verdicts

def _battery_candidate(source: str, goal_type: str) -> dict:
    # origin='battery' lets the thesis read-out separate a trivially-closed goal from one
    # carried by composition/retrieval. path='B' keeps compare/bank wiring unchanged.
    return {"source": source, "statement": f"example : {goal_type}",
            "proof_term": _bare_proof_term({"source": source}),
            "origin": "battery", "lean_type": goal_type, "path": "B"}
```

### 2. Wire into `verify_handler` Path B (the block at ~1215–1265, headed
`# ---- Path B: synthesized candidate, then the bounded repair loop ----`)

Run the battery FIRST; only pay for the LLM seed + repair when the battery yields no
win-eligible proof. Preserve the battery's `b_strong` counterfactual.

```python
weak = settings.prover_weak_path_b
b_strong = None
if research or verified_a is None:
    # Path B (deterministic): closer battery, regime-gated win-eligibility.
    bat_weak, bat_strong, bat_verdicts = _run_closer_battery(goal, weak=weak, profile=profile)
    for v in bat_verdicts:
        _record("Bdet", v["ok"])
    if bat_strong is not None:
        b_strong = _battery_candidate(bat_strong, goal)
    if bat_weak is not None:
        verified_b = _battery_candidate(bat_weak, goal)

    # Path B (synthesized): LLM seed + bounded repair — only when the battery didn't win.
    if verified_b is None:
        cb = _synthesized_candidate(ctx, sg_id)
        if cb:
            outcome = await prove_proposition(goal, cb["source"], weak=weak, profile=profile)
            for v in outcome.verdicts:
                _record("B" if v["path"] == "seed" else "B-repair", v["ok"])
            decision["repair_iters"] += outcome.repair_iters
            def _as_candidate(src): ...  # unchanged
            if outcome.source is not None and b_strong is None:   # keep battery's counterfactual
                b_strong = _as_candidate(outcome.source)
            if outcome.weak_source is not None:
                verified_b = _as_candidate(outcome.weak_source)
```

Everything downstream (`b_strong_closed`/`b_gated_out` dual read-out, `verified_a/_b`,
`compare`, stall/escalation, `bank`) is unchanged.

### 3. Tests (`tests/test_lean_prove_workflow.py`, `-m lean` tier for the kernel ones)
- `_closer_battery_tactics`: ∀/→ goal yields `intros; <c>` variants; plain goal does not.
- `_run_closer_battery` STRONG (`weak=False`): a `norm_num`-only goal ⇒ `weak_source==strong_source` non-None.
- `_run_closer_battery` WEAK (`weak=True`): on `((1:ℚ)/2+1/3)*(1/2-1/3)=5/36` ⇒ `strong_source` set
  (norm_num), `weak_source` None (primitives fail) — the control. On `2004 % 12 = 0` ⇒
  `weak_source` set (rfl) — honestly-trivial.

### 4. Open items to confirm before trusting numbers
- **How `research` mode got enabled in dev**: the last run's `verify_decision.mode == "research"`
  though `settings.prover_research_mode` defaults False. Find where dev eval flips it
  (`eval/demo.py` patches `prover_research_mode`; the API/eval_mode path may too) so you know the
  regime you're measuring.
- **How to toggle the weak regime**: `settings.prover_weak_path_b` is read **server-side** in the
  `hyperion` container (the benchmark client only POSTs). `Settings` is `pydantic-settings`
  (`config.py:67`, `model_config` at 262, no `env_prefix`) so an env var `PROVER_WEAK_PATH_B=true`
  on the `hyperion` service + `docker restart hyperion` should set it — VERIFY this maps, or wire
  it through the request/eval-mode. The benchmark CLI exposes no regime flag today.
- **Banking pollution (train only)**: a battery win (`origin="battery"`) is trivial; do NOT let it
  inflate the reuse-depth/snowball metric. In `dev` this is moot (episode/lemma writes are skipped:
  see `[eval] episode memory write skipped (eval_mode=dev)`). For `train`, consider skipping
  `abstract`/`bank` for `origin == "battery"` candidates. Not needed for the dev baseline.

---

## Environment / commands

- Repo root: `/Users/cep4u/personal-agent-theorem-proving`
- venv (use this): `agents/hyperion/.venv/bin/python`
- Hyperion API `http://localhost:4100` (`GET /config` liveness, no `/health`); Lean sidecar
  `http://localhost:8900` (`GET /health`).
- **If you edit `agents/hyperion/src`, `docker restart hyperion`** (bind-mounted, no auto-reload).
  The sidecar is a separate container — no rebuild needed for this change (Python prover only).
- Dev cases (3 rows, `lean_profile: mathlib`): `agents/hyperion/evals/lean_prove_splits/dev.jsonl`
  (`miniF2F-valid-mathd-{algebra-182, algebra-462, numbertheory-132}`).

### Quick kernel probe (sanity, sub-second)
```bash
curl -s -X POST http://localhost:8900/verify -H 'content-type: application/json' \
  -d '{"source":"import Mathlib\nexample : 2004 % 12 = 0 := by decide","mode":"full","profile":"mathlib"}'
```

### Unit tests
```bash
cd agents/hyperion
./.venv/bin/python -m pytest tests/test_lean_prove_workflow.py tests/test_plan_contract_lean.py -m "not lean" -q
./.venv/bin/python -m pytest -m lean -q     # kernel-backed tier (needs the live sidecar)
```

### Dev baseline — run BOTH regimes and report both numbers
```bash
cd agents/hyperion
# STRONG (default) — SOTA-comparison number
rm -f tasks/dev-results.jsonl
HYPERION_API_URL=http://localhost:4100 ./.venv/bin/python -m hyperion.eval.lean_prove_benchmark \
  --cases evals/lean_prove_splits/dev.jsonl --eval-mode dev --out tasks/dev-strong.jsonl --poll-seconds 5

# WEAK — thesis number (after enabling prover_weak_path_b server-side + docker restart hyperion)
HYPERION_API_URL=http://localhost:4100 ./.venv/bin/python -m hyperion.eval.lean_prove_benchmark \
  --cases evals/lean_prove_splits/dev.jsonl --eval-mode dev --out tasks/dev-weak.jsonl --poll-seconds 5
```

**Report per regime:** pass/fail per `case_id`; `final_verify`; and for each sub-goal the
winning `origin` (`battery` vs synthesized/composition vs retrieval) and `b_strong_closed`
(could a strong one-liner have closed a goal the weak prover couldn't win). The honest thesis
claim lives in the weak number + the composition/definition-mediated wins, NOT the battery wins.

Pull per-sub-goal detail from `ai-router/tasks/<task_id>/context.json` (keys
`candidate_b:<sg>`, `verify_decision:<sg>`, `discharged:<sg>`, `verified_b_strong:<sg>`) and
`progress.log`.

---

## Expected outcome
- Strong: **3/3** (battery closes all sub-goals).
- Weak: 132 closes (primitive `rfl`); 462 and 182 h1 fall to composition/definition — those are
  the goals whose closure is the thesis result, not the battery.

## Guardrails
- Never run the `test` split.
- Win-eligibility ONLY via `_uses_only_weak_tactics` — no hand-labelled "this is a weak tactic".
- Keep `formal_ingest` deterministic / no-LLM (no goal inference or retrieval).
- The kernel is the only judge of every battery attempt — a tactic that doesn't close is skipped;
  nothing is faked.
