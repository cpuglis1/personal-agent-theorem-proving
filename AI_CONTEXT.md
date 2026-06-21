# Shared AI Context & Handoff State

## 0. Current Handoff Note
- **NEW DIRECTION (active thrust): definition-mediated proving.** The research target is now
  `PLAN-definition-synthesis.md` — the system proves normally and, *only when stalled*, synthesizes
  a new local definition + kernel-verified bridge lemmas and proves *through* it; a definition is
  accepted as a concept only when **causally necessary across multiple theorems** (birth ablation +
  cross-theorem promotion). The existing prover/bank/necessity machinery is **reused as substrate**,
  not discarded. `PLAN-definition-synthesis.md` is the authoritative roadmap; the older
  `lean-prover-master-build-plan.md` / `hyperion-as-lean-prover-baseline.md` are the substrate's
  build history.
- **Phase 0 DONE (soundness foundation — the new baseline).** The non-negotiable `sorryAx` gate is
  implemented and offline-green:
  - Sidecar (`lean-sidecar/server.py`) has a new `POST /axioms {source, decl} -> {ok, axioms,
    errors}` endpoint: it appends `#print axioms <decl>`, elaborates, and parses the dependency list
    (handles single-line, line-wrapped, "does not depend on any axioms", `sorryAx`, and elaboration-
    failure shapes). The `lake env lean` run is factored into `_run_lean` and shared with `/verify`.
  - Client `tools/lean_verify.py` gained `lean_axioms(source, decl) -> AxiomsResult` with the same
    fail-soft contract as `verify_lean` (infra-down ⇒ `infra_ok=False`, never a false `ok=False`).
  - New `crews/soundness.py`: `axioms_clean(axioms, *, strict)` (subset of `SOUND_BASE = {propext,
    Classical.choice, Quot.sound}`; lax also tolerates native `Lean.ofReduceBool` etc., strict does
    not), `source_declares_gap(source)`, and `soundness_ok(source, decl, *, strict) ->
    SoundnessResult`. This is the single chokepoint to call at every acceptance/banking point.
  - Config knob `prover_soundness_strict: bool = False` (set True for headline runs).
  - Tests: `tests/test_soundness.py` + new `lean_axioms` cases in `tests/test_lean_verify.py`. Offline
    suite is green at **282 passed** (was 253). The `@pytest.mark.lean` live tier for `/axioms` +
    `soundness_ok` is written but requires a sidecar **rebuild** (`make lean-rebuild`) to ship the new
    endpoint — the running image predates it.
  - **Decision (recorded):** stay on the existing sidecar for the soundness check now; a Pantograph
    swap (plan's recommended interaction layer) is a later, isolated change.
- **Phase 1 DONE (proving primitive extracted).** `prove_proposition(goal_type, seed_source, *,
  weak=…, max_repair=…, decl=…, strict_soundness=…) -> ProofOutcome` now lives in
  `crews/lean_handlers.py` — the reusable kernel (verify seed → bounded `propose_repair` loop →
  weak-tactic gate → optional `soundness_ok` when a `decl` is supplied). `verify_handler` Path B was
  re-pointed at it with **no behavior change** (a `_as_candidate` wrapper rebuilds the blackboard
  dicts exactly as before). `ProofOutcome` carries `closed`/`source` (full-strength counterfactual),
  `weak_source` (win-eligible), `proof_term`, `repair_iters`, `verdicts`, `axioms`/`axioms_clean`,
  and a `.won` property. Tests: `tests/test_prove_proposition.py` (8). Offline suite **290 passed**.
  Bridges/planned-lemmas/ablation re-proofs in Phases 2-4 call this primitive.
- **Phase 2 DONE (definition synthesis).** The language-extending operation is wired (handlers
  registered; DAG wiring + stall-detection are Phase 4):
  - `config/agents/definition_synthesizer.json` — generalist concept former (`smart` alias, temp 0.5).
  - `propose_definition(stuck_lemma, informal_proof, lemma_plan, formalized_lemmas, lean_errors, n)`
    → `c` candidate concepts `{definition:{name,source}, bridges:[{name,source,lean_type,statement}],
    vacuity_probe?}` (scoped LLM call, mirrors `propose_repair`/`propose_abstraction`). Knob
    `concept_candidates=4`.
  - `definition_degeneracy_reasons(candidate, *, parent_name, parent_goal)` — pure, offline gates:
    no sorry/admit/axiom, not literal True/False, no parent-name mention, not defeq-to-parent (text),
    ≥1 bridge, bridges gap-free.
  - `synthesize_definition_handler` (native) — propose → gate → stage survivors to
    `concept_candidates:<sg>`; spends no proving budget.
  - `verify_concept_handler` (native) — elaborate def (no sorry) → Lean-backed non-vacuity probe
    (`example := by trivial` must FAIL) → prove EVERY bridge via `prove_proposition(decl=bridge.name)`
    soundness-clean (`won ∧ axioms_clean`); first fully-passing candidate → `verified_concept:<sg>`.
    No bank write (Phase 4).
  - Tests: `tests/test_concept_synthesis.py` (14). Offline suite **304 passed**.
- Next up: Phase 3 — birth ablation (`birth_ablation_handler`: re-prove the theorem through vs.
  without the concept at identical `B_ablate`; accept iff solves-with ∧ fails-without).
- _(prior handoff)_ This handoff is for switching to Claude Code after the Codex session usage expired.
- Docker is currently healthy. Running containers observed this session include `hyperion`, `hyperion-mcp`, `hyperion-ui`, `lean`, `litellm`, `qdrant`, `langfuse`, `searxng`, etc.
- The Lean sidecar is healthy: `POST http://localhost:8900/verify` with `{"source":"theorem t : True := trivial","mode":"full"}` returned `{"ok":true,"errors":[],"elaborated_term":null}`.
- Backend tests are green after the latest changes: `make hyperion-test` -> `253 passed, 69 warnings`.
- The live `lean-prove` smoke path is green. Latest smoke task was `14471e4f` for `Prove that 0 + 0 = 0.` and finished `done`; `artifacts/result.lean` was sorry-free and `bank` returned `ok=True`, `n_banked=1`, `bank_failures=[]`.
- Hyperion UI was stale in the running Docker image. Docker image rebuild failed due a local Docker credential-helper error resolving `node:20-alpine`, so the UI was hot-refreshed by running `VITE_HYPERION_API=http://localhost:4100 npx vite build` in `agents/hyperion-ui` and copying `dist/` into the running `hyperion-ui` container. `/prover/submit` should now render; if not, hard-refresh or use `http://localhost:4102/prover/submit?v=2`.

## 1. Project Overview & Tech Stack
- **Core Purpose:** This repo is a local-first AI workspace whose active branch is turning Hyperion, a CrewAI/FastAPI multi-agent orchestrator, into a Lean 4 theorem-proving system. The prover accepts a formal/natural theorem request, decomposes it into Lean sub-goals, races retrieval of banked lemmas against fresh synthesis, verifies with a Lean kernel sidecar, compares winners, abstracts fresh lemmas, and stores verified lemmas back into Qdrant so reuse improves over time.
- **Primary Tech Stack:** Python 3.12, CrewAI 0.86.0, FastAPI, aiosqlite, Qdrant, OpenAI-compatible LiteLLM proxy, Langfuse, SearXNG, Infinity reranker, Docker Compose, Lean 4/Mathlib sidecar, React 18 + Vite + TypeScript + Tailwind + React Query + React Router + Shiki + KaTeX for the UI. Package management is `uv` for backend and `npm` for frontend.
- **Key Entry Points:** Backend CLI scripts are `hyperion-api = hyperion.server.api:main` and `hyperion-mcp = hyperion.server.mcp:main` from `agents/hyperion/pyproject.toml`. Main API is `agents/hyperion/src/hyperion/server/api.py`; MCP server is `agents/hyperion/src/hyperion/server/mcp.py`; workflow runner is `agents/hyperion/src/hyperion/crews/runner.py`; workflow schema/validation is `agents/hyperion/src/hyperion/crews/workflows.py`; native deterministic nodes are registered via `agents/hyperion/src/hyperion/crews/native.py` and `agents/hyperion/src/hyperion/crews/lean_handlers.py`; Lean oracle client is `agents/hyperion/src/hyperion/tools/lean_verify.py`; sidecar service is `agents/hyperion/lean-sidecar/server.py`; prover workflow config is `agents/hyperion/config/workflows/lean-prove.json`; frontend app starts at `agents/hyperion-ui/src/main.tsx` / `src/App.tsx`, with prover views under `agents/hyperion-ui/src/pages/ProverSubmit.tsx` and `ProverRun.tsx`.

## 2. Current Architecture & Core Concepts
- Top-level infrastructure lives in `ai-router/`: LiteLLM (`:4000`), Qdrant (`:6333`), Langfuse (`:3001`), SearXNG (`:8888`), Infinity (`:7997`), Postgres, Open WebUI, Hyperion API (`:4100`), Hyperion MCP (`:4101`), and Hyperion UI (`:4102`) on the shared `ai-router_ai-net` Docker network. All LLM calls should go through LiteLLM, not directly to providers.
- Hyperion is a JSON-defined DAG runner. Workflow records live in `agents/hyperion/config/workflows/*.json`; agent records live in `agents/hyperion/config/agents/*.json`. `runner.py` topo-sorts nodes, groups independent nodes into parallel waves, handles HITL pauses/resume, writes progress/state to SQLite under `settings.tasks_dir/state.db`, and uses a per-task workspace under `settings.tasks_dir/<task_id>/`.
- Node kinds are `plan`, `work`, `synthesize`, `subworkflow`, and `native`. Agent nodes run CrewAI agents; `subworkflow` nodes run child workflows; `native` nodes run plain async Python handlers by registry key. Native nodes are the prover seam for deterministic control flow.
- The active Lean prover workflow is `lean-prove`: `decompose -> skeleton_check -> retrieve || synthesize -> verify -> compare -> abstract -> bank`. Retrieval (`Path A`) and synthesis (`Path B`) share the same upstream and therefore run in one parallel wave. `verify` waits for both.
- `decompose` is now a deterministic native handler, `lean_decompose`, not the flaky decomposer agent. It writes `<tasks_dir>/<task_id>/plan.md` directly with a conservative one-subgoal plan:
  - `selected_option: a`
  - subtask id `h1`
  - description `Prove the target proposition.`
  - `lean_type` extracted from `ctx.request` with `_prose_to_goal_type`
  - scaffold body:
    `have h1 : <goal> := sorry`
    `exact h1`
- The decomposer agent config is intentionally still present for future richer decomposition, but it is no longer on the critical smoke path.
- `plan.md` frontmatter is parsed by `crews/plan_contract.py`. Important prover fields are `scaffold` and `options[].subtasks[].lean_type`. The parser remains intentionally tolerant: it recovers from unclosed frontmatter, unquoted colons, scalar type coercion, and malformed older planner output.
- The Lean oracle is `tools/lean_verify.py`, which POSTs `{source, mode}` to `settings.lean_url + /verify`. `mode="skeleton"` permits `sorry`; `mode="full"` rejects `sorry`. The result distinguishes real proof failure from infrastructure failure with `infra_ok`. Callers must route on `infra_ok` first.
- The sidecar in `lean-sidecar/server.py` writes each source to a scratch Lean file and runs `lake env lean`. `docker-compose.lean.yml` builds/publishes it on host `:8900` and injects `LEAN_URL=http://lean:8900` into Hyperion services.
- Retrieval memory is now explicitly split into two roles:
  - `skill_library`: Hyperion-proved lemmas, instrumented for snowball metrics. New writes go here.
  - `mathlib_premises`: static traced Mathlib premise corpus, interface added but ingestion not implemented yet.
- `memory/lemma_bank.py` still exposes backward-compatible `store_lemma` / `retrieve_lemmas`, but those now mean the `skill_library` collection. It embeds lemma statements/types via `text-embedding-3-small`, uses deterministic UUID5 ids over normalized statements, self-creates the collection on first write, keeps reads fail-soft, and makes writes loud via `StoreResult`.
- New config fields in `config.py`:
  - `qdrant_skill_library_collection = "skill_library"`
  - `qdrant_mathlib_premises_collection = "mathlib_premises"`
  - `lemma_retrieval_mode = "skill"` (`skill | mathlib | combined`)
- `tools/lemma_retrieval.py` now supports retrieval modes, but the default is still `skill`, so current `lean-prove` behavior is unchanged except it reads from the new `skill_library` collection. `mathlib` and `combined` are plumbed but useful only after Mathlib premise ingestion.
- Applicability-aware retrieval remains: vector over-fetch, sparse symbol overlap/RRF, rerank via Infinity, then Lean-probe whether `exact h` / `apply h` can make progress. Verifier outages keep candidates rather than dropping them because the probe is inconclusive.
- Prover native handlers in `lean_handlers.py` write sub-goal-namespaced blackboard keys like `candidate_a:<sg>`, `candidate_b:<sg>`, `verified_a:<sg>`, `verified_b:<sg>`, `verify_decision:<sg>`, `triple_log:<sg>`, `discharged:<sg>`, and `abstracted:<sg>` into `context.json`.
- `verify_handler` is a deterministic controller, not an agent. It tries Path-A candidates in ranked order, then Path B and bounded repair (`settings.cap_repair_iters`). Repair proposals come from the `repair` agent through a scoped LiteLLM call, but only the Lean kernel can mark a proof as passing.
- `compare_handler` uses deterministic logic in `crews/lemma_compare.py` to choose the more reusable winner and logs the `(retrieved, synthesized, winner)` thesis triple. It now increments `times_won` best-effort for winning `skill_library` lemmas. Mathlib premises intentionally do not get win counters.
- `abstract_handler` asks the `abstractor` agent for most-general-first abstractions, re-verifies each with Lean, and falls back to the concrete lemma if all abstractions fail.
- `bank_handler` assembles `artifacts/result.lean` by replacing scaffold `sorry` holes with bare proof terms, full-verifies the assembled artifact when a scaffold exists, and stores winners in `skill_library`. It prefers the abstracted lemma for banking but uses the concrete discharge to fill the scaffold. Stored payloads now include `origin` and `source_collection`.
- Observability has two layers: LiteLLM/Langfuse for LLM calls, plus durable local traces. `usage.py` records native stages into `trace_events` so deterministic nodes show in Trace Flow; `eval/trace.py` reconstructs prover runs from `context.json`, `plan.md`, and `result.lean`; `eval/thesis_curve.py` aggregates triple logs into solved rate, Path-A win rate, contest win rate, and running retrieval curve.
- UI has a dedicated prover console. `/prover` renders fixture or live trace data, `/prover/submit` submits with workflow `lean-prove`, and `/prover/runs/:id` loads `GET /tasks/{id}/trace`. Lean code is highlighted with Shiki and math glyphs/LaTeX are handled via monospace font stack and KaTeX.

## 3. Current State & Recent Changes
- **What is working:** Core orchestration, JSON workflow validation, native-node dispatch, Lean oracle client contract, deterministic Lean decomposition, skill-library store/retrieve logic, retrieval-mode plumbing, applicability-aware retrieval, plan parsing hardening, verify/compare/abstract/bank handlers, prover trace reconstruction, thesis metric aggregation, MCP/API trace surfaces, and prover UI fixture/live views are implemented. Full backend suite: `make hyperion-test` -> `253 passed, 69 warnings`.
- **Live API smoke status:** `POST http://localhost:4100/tasks` with `{"task":"Prove that 0 + 0 = 0.","workflow":"lean-prove"}` succeeds. Latest task `14471e4f` fired all nodes: `decompose`, `skeleton_check`, `retrieve`, `synthesize`, `verify`, `compare`, `abstract`, `bank`. Because `skill_library` was fresh, `retrieve` found 0 candidates; Path B synthesized `rfl`; bank wrote 1/1 into `skill_library`.
- **Recent implementation changes in this session:**
  - Added native `lean_decompose_handler` in `agents/hyperion/src/hyperion/crews/lean_handlers.py`.
  - Updated `agents/hyperion/config/workflows/lean-prove.json` so `decompose` is `kind: native`, `handler: lean_decompose`.
  - Added scaffold wrapping through `_scaffold_as_command` so body-only scaffolds verify as `example : <goal> := by ...`.
  - Added `times_won` bumping for winners in compare/bank.
  - Split retrieval memory into `skill_library` and `mathlib_premises`, with retrieval modes `skill | mathlib | combined`. Default remains `skill`.
  - Added source/provenance fields: `origin`, `source_collection`, and Mathlib `premises_used` shape.
  - Added/updated offline tests in `test_lean_prove_workflow.py`, `test_lemma_bank.py`, `test_lemma_retrieval.py`, and `test_candidate_from_lemma.py`.
- **Dirty worktree warning:** There were already unrelated dirty files before this session, including Docker/sidecar/test files from prior work. Do not revert unrelated changes. Files definitely touched in this session include `lean_handlers.py`, `lemma_bank.py`, `lemma_retrieval.py`, `config.py`, `lean-prove.json`, and the tests listed above. `AI_CONTEXT.md` was also updated for this handoff.

## 4. Immediate Next Steps & Blockers
- **Roadmap reprioritized under the definition-synthesis thrust (`PLAN-definition-synthesis.md`).**
  The substrate items below (Mathlib ingestion, Lean-native retriever, local prover-model Path-B,
  symbolic anti-unification, ablation discipline) remain valid follow-ons but are **no longer the
  headline**. Active sequence: Phase 0 soundness gate **[done]** → Phase 1 `prove_proposition`
  extraction **[done]** → Phase 2 definition synthesis (`definition_synthesizer` agent +
  `synthesize_definition` / `verify_concept` handlers + degeneracy gates) **[done]** → Phase 3 birth
  ablation → Phase 4 DAG wiring + concept bank schema + stream-level promotion/pruning.
- [x] Definition synthesis (Phase 2): done. `definition_synthesizer` agent, `propose_definition`,
  `definition_degeneracy_reasons`, `synthesize_definition`/`verify_concept` native handlers. Offline 304.
- [x] Proving primitive (`prove_proposition`): done. Extracted from `verify_handler` Path B (no
  behavior change); `ProofOutcome` with strong/weak verdicts + soundness hook. Offline suite 290.
- [x] Soundness contract (`sorryAx` gate): done. Sidecar `/axioms`, `lean_axioms`, `crews/soundness.py`,
  `prover_soundness_strict` knob; offline suite green at 282. Live `/axioms` tier needs `make lean-rebuild`.
- [x] Deterministic decomposition: done. The former live blocker, decomposer/LiteLLM ReAct stall after `context_put`, is removed from the critical `lean-prove` smoke path.
- [x] Live sidecar smoke: done. `theorem t : True := trivial` verifies on `http://localhost:8900/verify`.
- [x] Backend tests: done. `make hyperion-test` -> `253 passed, 69 warnings`.
- [x] Live API smoke: done. Latest task `14471e4f` completed and banked 1/1 into `skill_library`.
- [x] Split-bank foundation: done. `skill_library` is active; `mathlib_premises` interface exists; retrieval modes exist with default `skill`.
- [ ] Next major implementation: Mathlib premise ingestion. Recommended direction: LeanDojo/LeanDojo-v2 tracing into `mathlib_premises` with payload `{name, statement/signature, lean_type, NL gloss if available, source, premises_used, symbol_set, provenance}`. Do not hand-roll this if LeanDojo can provide the trace.
- [ ] Next retrieval upgrade: replace/augment generic embeddings with a Lean-native retriever. Candidate directions discussed: ReProver `kaiyuy/leandojo-lean4-retriever-byt5-small`, LeanSearch/LeanSearch-PS, or LeanPremise. Keep current hybrid as baseline.
- [ ] Next synthesis upgrade: add a direct prover-model wrapper as a new Path B variant, preferably without CrewAI ReAct loops. Candidate: DeepSeek-Prover-V2-7B if local GPU permits. Keep current synthesizer as fallback.
- [ ] Next abstraction upgrade: keep current LLM abstraction verify-gated; later add statement-level symbolic anti-unification candidate generation. Do not anti-unify proof terms yet.
- [ ] Evaluation discipline: add ablations for `skill`, `mathlib`, `combined`, and no persistent library. Guard contamination: no published target solutions or validation-specific lemmas in the bank; use held-out years/problems for final validation.
- **Current Blockers:** No theorem workflow blocker after this session. The only operational blocker observed is frontend Docker image rebuild: `docker compose build hyperion-ui` failed due a local Docker credential-helper issue resolving `node:20-alpine`. Running UI was hot-refreshed manually. Backend services were restarted after code changes.

## 5. Shared Development Workflow
- **Build Command:** Backend install/build path is `cd agents/hyperion && .venv/bin/uv sync` if dependencies need refreshing. Backend services run with `make hyperion-api` and `make hyperion-mcp` from repo root, or `.venv/bin/uv run ...` inside `agents/hyperion`. Lean sidecar stack is rebuilt with `make lean-rebuild` from repo root; do not run `agents/hyperion/docker-compose.override.yml` standalone because it depends on services from `ai-router/docker-compose.yml`. Frontend build is `make ui-build`.
- **Test Command:** Backend normal command is `make hyperion-test`. To exclude live Lean integration explicitly, use `make hyperion-test-offline`; for focused prover coverage, use `make hyperion-test-prover`. Frontend test/build check is `make ui-build`. Lean sidecar smoke is `curl http://localhost:8900/health` and then a `POST /verify` with a trivial theorem.
- **Style Rules:** Keep changes additive and scoped to Hyperion's existing DAG/agent/native-handler pattern. Use native handlers for deterministic control flow and CrewAI/LiteLLM only for generative proposal steps. Never treat `infra_ok=False` as proof failure or proof success. Keep lemma-bank writes observable because they are load-bearing. Preserve the tolerant `plan_contract.py` parser behavior; planner output is expected to be imperfect. For tests, prefer offline mocks of Lean/LLM/Qdrant by default and reserve `@pytest.mark.lean` for real sidecar/kernel tests. For UI work, keep the prover console dense, operational, and trace-focused rather than marketing-oriented.
- **Quick manual smoke:** `curl -sS -X POST http://localhost:4100/tasks -H 'Content-Type: application/json' -d '{"task":"Prove that 0 + 0 = 0.","workflow":"lean-prove"}'`, then poll `/tasks/<id>` and inspect `/tasks/<id>/trace`. UI path is `http://localhost:4102/prover/submit`.
