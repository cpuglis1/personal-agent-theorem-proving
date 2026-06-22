# Hyperion UI — web console (`:4102`)

React + TypeScript + Vite + Tailwind front end for the Hyperion orchestrator
(FastAPI on `:4100`). Read-only consumer of the API.

## Run

```bash
cd agents/hyperion-ui
npm install
npm run dev        # Vite dev server on http://localhost:5173
```

> Port note: Vite is pinned to **5173** internally; the project map publishes the
> console on **:4102** via the Docker/run layer (which maps 4102 → 5173). Open
> `http://localhost:5173` for local dev, or `:4102` when running the Docker stack.

`npm run build` type-checks (`tsc --noEmit`) and produces a production bundle.

The backend base URL defaults to `http://localhost:4100`; override with the
`VITE_HYPERION_API` env var at build/dev time.

---

## Prover console (`/prover`)

A math/Lean-friendly view of what each stage of a Lean-4 proof run produced. The
backend prover, per sub-goal, races two paths — **Path A "retrieve"** (reuse a
banked lemma) ‖ **Path B "synthesize"** (write a fresh proof) — then **verify**
(Lean kernel verdict + bounded repair). If that basic path stalls, the run can
escalate through **definition synthesis**, **verify_concept**, and
**prove_through** before **bank** assembles the final `result.lean`.

Three views:

| Route | What |
| --- | --- |
| `/prover` | **Run view** (centerpiece). Per-subgoal pipeline cards: retrieve → synthesize → verify → concept escalation/prove-through → discharged, with the winning path and repair iterations. Shows the **scaffold** and final **`result.lean`** prominently, plus a compact read-out (solved-rate, Path-A retrieval win-rate, Path-C concept wins). |
| `/prover/submit` | **Submit view**. POST a theorem with workflow `lean-prove`; returns a `task_id` and links to its live Run view. |
| `/prover/runs/:id` | The Run view bound to a specific live `task_id`. |

### Fixture vs. live toggle

The Run view has a **data-source toggle**:

- **Fixture** (default, no backend) — renders from
  [`fixtures/sample-trace.json`](fixtures/sample-trace.json), the real `prover`
  payload shape. `npm run dev` shows the full Run view offline.
- **Live (`:4100`)** — paste a `task_id` (or arrive via `/prover/runs/:id`) to
  fetch `GET /tasks/{id}/trace` from the backend. If the Docker stack is down,
  the view shows a graceful error instead of crashing.

### Math-friendly rendering

- **Lean 4 source** (scaffold, candidate `source`/`statement`/`proof_term`,
  `result.lean`) is syntax-highlighted with [Shiki](https://shiki.style) using
  its `lean4` grammar (fine-grained core import — only that grammar is bundled).
  Falls back to a styled `<pre>` if Shiki is unavailable.
- Lean's unicode-heavy glyphs (`∀ ∃ ∧ ∨ → ↔ ¬ ≤ ≥ ∈ ℕ ℝ ℤ λ ⟨ ⟩ ⊢ α β γ Π Σ`)
  render in a monospace stack with full math coverage:
  **JuliaMono** (if installed) → **JetBrains Mono** (loaded from Google Fonts when
  online) → **Menlo** (macOS fallback).
- `$…$` / `$$…$$` LaTeX in descriptions renders via
  [KaTeX](https://katex.org).
- Code blocks have a copy button and scroll horizontally (no wrapping).

> Visual check: after `npm run dev`, open `/prover` — the scaffold and
> `result.lean` should be highlighted Lean, and the subgoal cards should show
> Path A / Path B / Path C status without compare or abstraction rows.
