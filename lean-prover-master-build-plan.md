# Master Build Plan — Transforming Hyperion into a Lean 4 Theorem-Proving System

**Status:** build playbook (the *how*). Companion to [hyperion-as-lean-prover-baseline.md](hyperion-as-lean-prover-baseline.md) (the *why/what*).
**Audience:** the implementing agent (me) and any human reviewing the build.
**Repo root:** `/Users/cep4u/personal-agent-theorem-proving` — Hyperion lives at [agents/hyperion/](agents/hyperion/).

---

## 0. How to use this document

The baseline doc established **what** to reuse, tune, and build. This document is the **ordered, test-gated build sequence** that turns that analysis into code. It is structured as:

- **Pre-work** — fork hygiene, the regression safety net, de-personalization, and the one toolchain decision that gates everything else.
- **Phase 1 → 5** — five build phases. Each delivers **one testable component** and ends with a **Test Gate** (a concrete `tests/test_*.py` and a binary *definition of done*). No phase starts until the prior phase's gate is green.
- **Post-work** — measurement harness, the research/deploy policy knob, cap re-tuning, and turning the bank load-bearing.

**Two rules that govern every phase:**

1. **The existing orchestration tests are sacred.** `uv run pytest` (15 files under [agents/hyperion/tests/](agents/hyperion/tests/)) is the regression net. It must stay green at every commit. We *extend* the runner; we never restructure it.
2. **Test the component the moment it exists, in isolation, offline-first.** Mirror the repo's own test philosophy (mock Lean/LLM/Qdrant, `tmp_path` + patched `settings`, `@pytest.mark.anyio`). A component without a green gate does not exist.

### Build-phase vs. prover runtime-phase — do not confuse them

The prover *runs* in 5 runtime phases (baseline §2). We *build* in 5 build phases, in a **different order** (oracle first, novel module last). This table is the Rosetta Stone:

| Build Phase (this doc) | Delivers | Prover runtime phase it powers (baseline §2/§5) |
|---|---|---|
| **Pre-work** | Green baseline, de-personalized fork, toolchain decision | — (infrastructure) |
| **Phase 1** | Lean verifier tool + sidecar; native-node runner seam | P3 oracle (and P1 skeleton check, P4 re-verify) |
| **Phase 2** | `lemma_bank.py` (re-skinned episodic) | P5 Bank |
| **Phase 3** | Applicability-aware lemma retrieval | P2 Path A (retrieve) |
| **Phase 4** | `lean-prove` workflow: decompose → retrieve‖synthesize → verify → bank (NO abstraction) | P1, P2 Path B, P3 verify/compare loop |
| **Phase 5** | Compare + triple-log + anti-unification abstractor | P3 compare, P4 Abstract |
| **Post-work** | Thesis-curve harness, policy knob, cap re-tuning | Measurement + RESEARCH/DEPLOY knob |

> The point of building **oracle-first** is that nothing downstream can be tested end-to-end without a real verifier, and the abstractor (the novel research contribution) is built **last** because it needs everything beneath it to be solid before it can be tuned.

---

## 1. The one architectural decision: native (plain-Python) nodes

Read this before Phase 1; it shapes Phases 1, 4, and 5.

Today the runner's per-node dispatch ([runner.py:847](agents/hyperion/src/hyperion/crews/runner.py#L847), `_run_one`) knows exactly two node kinds:

- `kind == "subworkflow"` → run a nested workflow in plain Python (`_run_subworkflow`)
- everything else → build a CrewAI agent and run a crew (`build_agent` + `_run_stage`)

The prover needs several steps whose **control flow is deterministic** to be first-class DAG citizens — `verify`, `compare`, `bank`, and the Path-A `retrieve` step. `compare`, `bank`, and `retrieve` are fully non-LLM; `verify` is a *controller* that owns a deterministic loop but delegates one generative sub-step — **repair** — to an LLM (see §1a). Baseline risk #1 is explicit: a tight `propose → lake build → repair` cycle should **not** be a CrewAI ReAct agent. But the baseline's other suggestion (call plain functions *between* stages) sacrifices the thing that makes Hyperion worth reusing: **the DAG is the single source of control flow.**

**Decision: add a `"native"` node kind** that dispatches to a registered plain-Python handler, exactly parallel to how `"subworkflow"` already dispatches to `_run_subworkflow`. This is the *only* structural change to the runner in the whole build, it is small, and it generalizes (verify/compare/bank/retrieve are all the same shape).

**Concrete contract (Phase 1 delivers the seam, Phases 4–5 register handlers):**

```python
# workflows.py — extend the Literal and validation
NodeKind = Literal["plan", "work", "synthesize", "subworkflow", "native"]
# A native node sets `handler` (a registry key), leaves `agent`/`workflow` unset.
# validate_workflow enforces the new exactly-one-of, mirroring the subworkflow rule.

# A new native-handler registry, mirroring TOOL_REGISTRY in agents/registry.py:
#   NATIVE_HANDLERS: dict[str, Callable[[NativeNodeCtx], Awaitable[NativeResult]]]
#   register_native_handler("retrieve", retrieve_handler) # Phase 4
#   register_native_handler("verify", verify_handler)   # Phase 4 — controller; calls the `repair` agent (§1a)
#   register_native_handler("compare", compare_handler) # Phase 5
#   register_native_handler("bank", bank_handler)       # Phase 5

# runner._run_one — one new branch, parallel to the subworkflow branch:
if n.kind == "native":
    res = await run_native_node(n, ctx)   # reads/writes blackboard + workspace
    return n.id, res
```

Handlers receive a small typed context (`task_id`, the node, the blackboard accessors, the sub-goal) and return a result dict the runner records exactly like a stage output. They get the **same** `CapExceeded`/wall-budget protection because they run inside the same `try` in `_execute_workflow` ([runner.py:797](agents/hyperion/src/hyperion/crews/runner.py#L797)).

Why this is safe: it is additive. Existing workflows declare no `native` nodes, so `_wave_groups`, `topo_sort`, gating, resumability, and every existing test behave identically. **Its own Test Gate is in Phase 1.**

### 1a. The `verify` node: a native *controller* with a generative `repair` agent

`verify` is the one native node that is **not** pure plumbing. Reading Lean errors and reasoning about a fix *is* real LLM judgment — but that does not make the whole node an agent (Approach A's mistake was bundling). Pull the verify/repair cycle apart and it has three jobs of three different natures:

| Job | Nature | Owner |
|---|---|---|
| **Verdict** — `verify_lean(proof) → {ok, errors}` | pure oracle, zero judgment | **native** — never an LLM; the verdict must stay ground truth |
| **Routing** — ok → discharge; failed & budget left → repair; both candidates fail → Path A next-best vs Path B repair | deterministic control flow | **native** — the handler |
| **Proposal** — read the errors, reason about a fix in prose, emit a new candidate | generative judgment | **a `repair` agent**, invoked by the handler |

The handler owns a deterministic loop and delegates only the proposal:

```python
async def verify_handler(ctx) -> NativeResult:
    candidate = ctx.blackboard.get("candidate")        # from synthesize / retrieve
    goal      = ctx.subgoal.lean_type
    for attempt in range(settings.cap_repair_iters):   # bounded; CapExceeded-class guard
        res = verify_lean(candidate, mode="full")      # NATIVE — verdict untouched, cannot be faked
        if res.ok:
            return passed(candidate, res, attempts=attempt)
        candidate = await propose_repair(goal, candidate, res.errors)  # GENERATIVE — the `repair` agent
    return failed(res, attempts=settings.cap_repair_iters)            # give up cleanly → Path A may still win
```

- **`propose_repair`** invokes a thin **`repair` agent record** (model/prompt/temperature configurable in JSON/UI), **one proposal per call**. The handler — not the agent — owns iteration, so the loop stays observable and precisely capped. (A scoped structured LLM call, à la `_summarize_context` at [runner.py:149](agents/hyperion/src/hyperion/crews/runner.py#L149), is the lighter alternative when agent-level configurability isn't needed.)
- **Critical invariant:** the model *proposes*, the kernel *judges*, and every proposal is checked on the very next line. An LLM can be arbitrarily creative and still cannot hallucinate a pass.
- **Why repair is invoked-by-node, not its own node:** a repair loop is a *cycle*, and the DAG is acyclic (`topo_sort` forbids back-edges, [workflows.py:285](agents/hyperion/src/hyperion/crews/workflows.py#L285)). Owning the loop inside the handler sidesteps acyclicity entirely; making repair a peer node would force iteration/back-edges into the runner — a *bigger* change than the native seam, breaking "one structural change only."
- **`cap_repair_iters`** is a new `settings` field (Post-work tunes it against measured sidecar latency). It backstops the repair loop the way `cap_tool_loop` ([runner.py:46](agents/hyperion/src/hyperion/crews/runner.py#L46)) backstops ReAct loops.
- **Measurement payoff:** because the loop is native, every `verify_lean` call and repair attempt is observable and logged — feeding the thesis triple log (repair iterations-to-close, repair vs. retrieval win-rate). A loop hidden inside a ReAct agent would forfeit that visibility.
- **Upgrade path:** if single-shot repair underperforms, swap `propose_repair`'s body for a fully autonomous repair agent that owns its own inner loop (calls `lean_verify` as a tool). Richer per-activation reasoning, less loop observability — same seam, only the body changes.

**Which kind is each prover step:**

| Step | Kind |
|---|---|
| decompose / synthesize / abstract | CrewAI agent (generative core) |
| retrieve / compare / bank | native (deterministic) |
| **verify** | **native controller** — owns the loop + routing, calls the `repair` agent |
| **repair** | **agent, invoked by `verify`** — error-reading + repair strategy in prose |

---

## 2. Testing philosophy (the critical through-line)

The user's hard requirement: **test each core component as it is built.** The repo already models exactly how. We adopt its conventions verbatim and add one tier.

### Three test tiers

| Tier | Marker / location | Touches | Runs in CI? | Mirrors |
|---|---|---|---|---|
| **Unit (offline)** | `tests/test_*.py`, default | Nothing external. Mock Lean, LLM, Qdrant, httpx. | Yes, always | [test_tools.py](agents/hyperion/tests/test_tools.py) |
| **Orchestration (offline)** | `tests/test_*.py`, `@pytest.mark.anyio` | Runner with mocked `_run_stage`/handlers + `tmp_path` | Yes, always | [test_subworkflow.py](agents/hyperion/tests/test_subworkflow.py) |
| **Integration (live Lean)** | `@pytest.mark.lean`, skipped if `lake` absent | A **real** Lean toolchain / the sidecar | Nightly / on-demand | *new tier* |

The integration tier is new because, unlike Hyperion's LLM critic, **our oracle is real, cheap, and fast** (baseline risk #3) — so we *can and must* test against actual Lean, not only a mock. Gate it behind a marker so the default `uv run pytest` stays hermetic and fast:

```python
# conftest.py addition
def pytest_collection_modifyitems(config, items):
    if shutil.which("lake") is None:
        skip = pytest.mark.skip(reason="Lean toolchain (lake) not installed")
        for item in items:
            if "lean" in item.keywords:
                item.add_marker(skip)
```

### The standard fixtures (copy from the existing suite)

- `anyio_backend` fixture returning `"asyncio"` (the runner is asyncio-based) — from [test_subworkflow.py:43](agents/hyperion/tests/test_subworkflow.py#L43).
- `patch.object(settings, "tasks_dir", tmp_path)` for any on-disk test — from [test_tools.py:63](agents/hyperion/tests/test_tools.py#L63).
- Mock the network at `httpx.post`/`httpx.get` for service clients — from [test_tools.py:115](agents/hyperion/tests/test_tools.py#L115).
- A `_mock_lean` context manager (Phase 1 deliverable) that patches the verifier to return canned `{ok, errors, elaborated_term}` so every downstream phase can run LLM-free **and** Lean-free.

### The Test Gate contract

Every phase below ends with a **Test Gate**: a named test file, the specific behaviors under test, and a **Definition of Done** — a binary checklist. *The phase is not complete until every box is checked and `uv run pytest` is green.* This is the enforcement mechanism for "test each component as it is built."

---

## Pre-work — Fork hygiene & the safety net

**Goal:** a clean, de-personalized fork with a green regression net and a settled Lean-toolchain plan. No prover code yet.

### Steps

1. **Provenance & license (blocking).** Baseline risk #6. The code is authored by "Charlie Tolleson" ([pyproject.toml](agents/hyperion/pyproject.toml)). Confirm `LICENSE` and git history grant the right to fork/derive before any commercial work. **Do not proceed past this step until resolved.**
2. **Green the baseline.** `cd agents/hyperion && uv run pytest`. All 15 files must pass *before* touching anything. This is the net that tells you when you've broken orchestration. Record the baseline pass count.
3. **De-personalize (baseline risk #5).** Strip the second-brain / Notion / Obsidian assumptions and the author's personal vault. Concretely:
   - Hardcoded collection names `second_brain`, `hyperion_memory` ([episodic.py:45](agents/hyperion/src/hyperion/memory/episodic.py#L45)) → config-driven, prover-namespaced (`lemma_bank`).
   - The `importlib` shim for the personal `_shared/qdrant_client.py` ([second_brain.py:39-49](agents/hyperion/src/hyperion/tools/second_brain.py#L39-L49)) — decide: keep `second_brain` as a generic retrieval tool, or remove it. For the prover it is dead weight; recommend removing the agent's access and leaving the module dormant.
   - `SecondBrainTool` / `NotionWriteTool` registrations in `TOOL_REGISTRY` ([registry.py:39-51](agents/hyperion/src/hyperion/agents/registry.py#L39-L51)) — leave registered (harmless) but don't grant them to prover agents.
4. **Toolchain decision (gates Phase 1).** Pick the Lean execution strategy now — it is the longest pole (baseline §6.2):
   - **Recommended: a Lean *sidecar* service** on `ai-net`, long-lived, with a **warm Mathlib cache** baked into its image. Cold-building Mathlib per sub-goal is fatal to wall-time. The compose file already mounts `/var/run/docker.sock:ro` "so Hyperion can spawn/inspect sibling containers (e.g. sandboxed tool execution)" ([docker-compose.override.yml:60-61](agents/hyperion/docker-compose.override.yml#L60-L61)) — this is the intended hook.
   - Alternative: bake `elan`/`lake`/`lean` into the `hyperion` image. Simpler topology, but every API container carries the (large) toolchain and there's no shared warm cache. Reject unless the sidecar proves troublesome.

   > **DECISION (2026-06-17): Lean sidecar service.** Confirmed against the compose
   > conventions (`litellm`/`qdrant`/`infinity` are wired by service name with `*_URL`
   > env vars and `depends_on` on the external `ai-router_ai-net` network). The Lean
   > service slots in identically via a new `docker-compose.lean.yml` override.
   > - **Version pin:** Mathlib is pinned to release tag **`v4.15.0`**, and the exact
   >   **Lean toolchain is derived from Mathlib's own `lean-toolchain` file** (installed
   >   via `elan`), so Lean and Mathlib can never drift out of compatibility. The pin is
   >   bumpable: change the Mathlib tag and rebuild the image.
   > - **Warm cache:** `lake exe cache get` is run at **image-build time** so the
   >   prebuilt `.olean`s are baked into a layer (mitigates risk B2 — Mathlib cold-build
   >   wall-time). No per-sub-goal Mathlib build.
   > - **Topology:** service `lean` on `ai-net`, `LEAN_URL: "http://lean:8900"`, exposing
   >   `POST /verify {source, mode} → LeanResult`. No `docker.sock` mount (it is a peer
   >   service, not a spawned sibling container).
   > - **Setting:** `lean_url` added to [config.py](agents/hyperion/src/hyperion/config.py)
   >   parallel to `infinity_url`; `LEAN_URL` env maps by attribute name.
5. **Rename for the new domain.** New service identity in `pyproject.toml`/README where it won't fight the existing infra (the package can stay `hyperion` internally to avoid churn; the *product* is the Lean prover).

### Test Gate — Pre-work

- **File:** none new; the existing suite.
- **Definition of Done:**
  - [x] License/provenance confirmed in writing. *(2026-06-17; recorded in [PROVENANCE.md](PROVENANCE.md).)*
  - [x] `uv run pytest` green; baseline pass count recorded. *(**110 passed**, run via `agents/hyperion/.venv/bin/uv` since `uv` is not on the non-interactive PATH.)*
  - [x] Collection names are config-driven, not hardcoded literals; a grep for `second_brain`/`hyperion_memory` string literals returns only config defaults. *(`qdrant_memory_collection`/`qdrant_lemma_collection` in config.py; `episodic.py` reads them. The lone `second_brain` literal in `registry.py:44` is a tool-registry key, not a collection name.)*
  - [x] Toolchain strategy chosen and written into this doc (sidecar vs. baked). *(Sidecar; Mathlib `v4.15.0`, Lean derived from its `lean-toolchain`; see step 4 DECISION.)*
  - [ ] Prover agents (to be authored) will not reference second-brain/Notion tools. *(Deferred to Phase 4 when agent records are authored.)*

---

## Phase 1 — The Lean Oracle (verifier tool + sidecar + native-node seam)

**This is the critical path and the longest pole. Nothing downstream is end-to-end testable without it.** (Baseline §6.2.)

**Goal:** "submit a `.lean` string → get back `{ok, errors, elaborated_term}`" working standalone, plus the `native` node seam so deterministic steps can live in the DAG.

### Deliverables

1. **`tools/lean_verify.py`** — a plain function `verify_lean(source, *, mode) -> LeanResult` **and** a thin CrewAI `BaseTool` wrapper. Follow the `BaseTool` + `_safe_path` sandbox pattern from [workspace.py](agents/hyperion/src/hyperion/tools/workspace.py). The plain function is what native nodes call directly (no ReAct loop, per the §1 decision). Contract:
   ```python
   class LeanResult(TypedDict):
       ok: bool
       errors: list[str]          # parsed compiler diagnostics
       elaborated_term: str | None
       mode: Literal["skeleton", "full"]
   # skeleton mode: `sorry` elaborates; checks the have-chain composes to the target (P1).
   # full mode: no `sorry` permitted; the proof must close (P3/P4).
   ```
   It writes the candidate into a Lean project, invokes the sidecar (or `lake build`), parses diagnostics, returns the dict. **Fail-soft on infra, hard-fail on Lean errors** — distinguish "the verifier service is down" (retryable, like [reranker.py](agents/hyperion/src/hyperion/tools/reranker.py)'s degrade) from "the proof doesn't type-check" (a real `ok=False`). These must never be conflated.
2. **Lean sidecar service** in a new `docker-compose.lean.yml` override (or extend the existing one): a long-lived container on `ai-net` with a warm Mathlib cache, exposing a minimal HTTP endpoint (`POST /verify {source, mode} → LeanResult`). Mirror the env/URL convention (`LEAN_URL: "http://lean:NNNN"`) used for `litellm`/`qdrant`/`infinity` in [docker-compose.override.yml](agents/hyperion/docker-compose.override.yml).
3. **`LEAN_URL` setting** in [config.py](agents/hyperion/src/hyperion/config.py) (parallel to `infinity_url`).
4. **Register the tool** in `TOOL_REGISTRY` ([registry.py:39](agents/hyperion/src/hyperion/agents/registry.py#L39)) as `lean_verify`.
5. **The native-node seam** (§1): extend `NodeKind`, `validate_workflow` (the exactly-one-of rule, mirroring the subworkflow branch at [workflows.py:361-385](agents/hyperion/src/hyperion/crews/workflows.py#L361-L385)), the `NATIVE_HANDLERS` registry, and the one new branch in `_run_one` ([runner.py:847](agents/hyperion/src/hyperion/crews/runner.py#L847)). Ship it with a trivial echo handler so it's testable now; real handlers land in Phases 4–5.
6. **`_mock_lean` test helper** so all later phases run Lean-free.

### Test Gate — Phase 1

- **Files:** `tests/test_lean_verify.py` (unit + integration), `tests/test_native_node.py` (orchestration).
- **Under test:**
  - *Unit (offline, mock the sidecar HTTP):* a passing proof → `ok=True`; a type error → `ok=False` with parsed `errors`; **service-down degrades distinctly** from a Lean error (the load-bearing distinction); skeleton mode accepts `sorry`, full mode rejects it.
  - *Integration (`@pytest.mark.lean`, real toolchain):* a known-true toy theorem verifies; a deliberately broken proof fails with a real diagnostic; warm-cache round-trip latency is recorded (feeds Post-work cap tuning).
  - *Orchestration:* a one-node `native` workflow runs its handler, records routing, and respects `CapExceeded`/wall budget; existing subworkflow/agent dispatch is unchanged (run the full suite).
- **Definition of Done:**
  - [~] `verify_lean("theorem t : True := trivial", mode="full")` → `ok=True` against the real sidecar. *(Test written: `test_real_true_theorem_verifies` (`@pytest.mark.lean`). **Deferred** — needs the built Mathlib sidecar + `lake`; skipped in this env.)*
  - [~] A `sorry`-containing skeleton passes in `skeleton` mode and fails in `full` mode. *(Test written: `test_real_sorry_skeleton_vs_full` (`@pytest.mark.lean`). **Deferred** — live tier.)*
  - [x] Sidecar-unreachable returns a *retryable infra* signal, never a false `ok=False`. *(`test_service_down_degrades_distinctly_not_false_ok` — **verified offline**.)*
  - [x] `native` node executes end-to-end in the runner; all 15 pre-existing test files still pass. *(`test_native_node_runs_end_to_end_in_runner`; full suite **131 passed, 4 skipped**.)*
  - [x] `_mock_lean` helper exists and is importable by other test modules. *(`tests/lean_mock.py`, `from lean_mock import mock_lean`.)*

  > **Live-Lean tier status (this environment):** `lake` is not installed and the
  > Mathlib sidecar image is not built here, so the 4 `@pytest.mark.lean` tests are
  > skipped (by design — conftest gates them). They are written and ready; the two
  > sidecar-dependent DoD boxes above are marked `[~]` (deferred) until the image is
  > built and `uv run pytest -m lean` is run against the running sidecar. **All
  > offline DoD boxes are green.**

---

## Phase 2 — The Lemma Bank (re-skin episodic memory)

**Goal:** `memory/lemma_bank.py` — Qdrant-backed store/retrieve/dedup for verified lemmas. Re-skin, don't rewrite. (Baseline §3 "tune", §6.3.)

### Deliverables

Copy the skeleton of [episodic.py](agents/hyperion/src/hyperion/memory/episodic.py) and change three things; keep the rest (lazy client imports, deterministic upsert, fail-soft posture — *but see the load-bearing caveat below*):

1. **Embedding text** → the **lemma statement / goal type**, not `request + summary` ([episodic.py:71](agents/hyperion/src/hyperion/memory/episodic.py#L71)).
2. **Payload schema** → `{statement, proof_term, generality_score, source_goal, verified_at, verification_mode}` (replaces the task-episode payload at [episodic.py:80-89](agents/hyperion/src/hyperion/memory/episodic.py#L80-L89)).
3. **Dedup identity** → UUID5 over the **normalized lemma statement** (not `task_id`), so re-deriving the same lemma upserts instead of duplicating ([episodic.py:77-78](agents/hyperion/src/hyperion/memory/episodic.py#L77-L78)). Near-duplicate skip = `score_threshold` check on upsert.
4. **API** → `store_lemma(...)` and `retrieve_lemmas(goal, limit)` (replacing `store_episode` / `recall_similar_tasks`).
5. **Collection** → `lemma_bank` (config-driven from Pre-work).

> **Load-bearing decision (baseline risk #4):** episodic memory swallows all errors because "memory is a nice-to-have." For the prover, **a failed bank write loses a verified lemma and stalls the snowball** — it is load-bearing for the thesis. Decide the posture *now*: recommend keeping the fail-soft *read* path but making the *write* path **loud** (log at error, surface to the run result, optionally retry). Phase 5's bank handler depends on this choice.

### Test Gate — Phase 2

- **File:** `tests/test_lemma_bank.py` (unit, mock Qdrant + embeddings).
- **Under test:** store→retrieve round-trip; UUID5 dedup (same normalized statement upserts to one point); near-duplicate skip honors `score_threshold`; **write failure is observable** (not silently swallowed) per the load-bearing decision; statement normalization is stable (whitespace/alpha-equivalence as scoped).
- **Definition of Done:**
  - [x] Storing the same lemma twice yields one Qdrant point (deterministic UUID5). *(`test_same_lemma_twice_is_one_point`; UUID5 over the whitespace-normalized statement, `test_dedup_id_is_whitespace_normalized`.)*
  - [x] `retrieve_lemmas` returns payloads ranked by vector score. *(`test_retrieve_returns_payloads_ranked_by_score`.)*
  - [x] A simulated write failure is logged at error and surfaced — proven by test, not by inspection. *(`store_lemma` returns `StoreResult(ok=False, error=...)` and logs at ERROR; `test_write_failure_is_loud_and_observable`. Reads stay fail-soft: `test_retrieve_failure_is_fail_soft_returns_empty`.)*
  - [x] No live Qdrant required to run the suite. *(All 9 tests mock `_get_clients`; full suite **140 passed, 4 skipped**.)*

  > **Near-duplicate `score_threshold`-on-upsert skip** (deliverable #3) is **deferred**:
  > exact-UUID5 dedup is the load-bearing identity for this gate, and semantic
  > near-duplicate collapse is better tuned alongside the Phase 3 applicability gate.
  > True alpha-equivalence normalization (needs Lean binder parsing) is likewise out of
  > scope here; normalization is scoped to whitespace (`lemma_bank._normalize`).

---

## Phase 3 — Applicability-aware lemma retrieval (Path A)

**Goal:** the Path-A retrieval step: embed goal → Qdrant `lemma_bank` → rerank → **applicability gate**. (Baseline §3, risk #2.)

### Deliverables

1. **Retrieval pipeline** reusing the over-fetch → rerank → budget-trim shape of [second_brain.py](agents/hyperion/src/hyperion/tools/second_brain.py) and the `prioritize` primitive in [reranker.py:118](agents/hyperion/src/hyperion/tools/reranker.py#L118). Source is `lemma_bank`, not `second_brain`.
2. **The applicability gate (the new, non-obvious part — baseline risk #2):** textual rerank relevance ≠ logical applicability. After reranking, add a cheap Lean-aware filter: *does `apply <lemma>` / `exact <lemma>` make progress on the goal?* Implemented via Phase 1's `verify_lean` in a lightweight probe mode. The reranker stays as the coarse pre-filter; the gate is the precision pass. Candidates that rerank well but don't unify are dropped.
3. Expose as both a plain function (native nodes call it) and optionally a `BaseTool`.

### Test Gate — Phase 3

- **Files:** `tests/test_lemma_retrieval.py` (unit; mock Qdrant, mock reranker via `httpx`, mock `verify_lean` with `_mock_lean`).
- **Under test:** ranking order from the reranker is honored; the applicability gate **drops a textually-similar-but-non-applying lemma** and **keeps an applying one** (the core correctness claim of this phase); reranker-down degrades to vector order (fail-soft, like [test_tools.py:122](agents/hyperion/tests/test_tools.py#L122)); token-budget trim preserved.
- **Definition of Done:**
  - [ ] A crafted case where the top *textual* match does **not** apply and a lower one does → retrieval returns the applying lemma first.
  - [ ] With the reranker mocked down, retrieval still returns vector-ordered candidates.
  - [ ] Runs fully offline.

---

## Phase 4 — The prover workflow (decompose → retrieve‖synthesize → verify → bank)

**Goal:** a working **retrieve-race-verify-bank loop with NO abstraction.** Baseline §6.4: "a working retrieve-race-verify-bank loop is already a real system." This is the first end-to-end prover.

### Deliverables

1. **Plan-contract extension** ([plan_contract.py](agents/hyperion/src/hyperion/crews/plan_contract.py)): reuse the tolerant frontmatter mechanism; add prover semantics. `subtasks` carry a **`lean_type`** per `have ... := sorry` sub-goal (not just a description); `options` = alternative decompositions; add a **`scaffold`** field holding the have-chain proof text so the P1 skeleton check has something to type-check. Keep the parser tolerant (never raises) — that property is load-bearing for the runner.
2. **Agent records** (JSON under [config/agents/](agents/hyperion/config/agents/), templated on [planner.json](agents/hyperion/config/agents/planner.json)):
   - `decomposer` — a re-prompted planner emitting the extended contract (`kind: plan`).
   - `synthesizer` — Path B; writes a bespoke lemma for the exact goal (`worker` model, the real cost).
   - `repair` — invoked by the `verify` handler (§1a), **one proposal per call**: reads the Lean errors and emits a revised candidate. Configurable model/prompt; `smart` or a math-tuned model is appropriate here. This is where the LLM judgment over compiler diagnostics lives.
   - (`retrieve`, `verify`, `bank` are **native** nodes, not agents — `verify` is a *controller* that *calls* the `repair` agent; see §1a.)
3. **Native handlers** registered (the §1 seam from Phase 1):
   - `retrieve` → Phase 3 pipeline.
   - `verify` → the **native controller** (§1a): `verify_lean` both candidates; on a pass, discharge the sub-goal; on BOTH FAIL, **deterministic routing** — Path A takes its next-best rerank match, and Path B calls the **`repair` agent** for a fresh proposal then re-verifies, looping up to **`cap_repair_iters`** (a new `settings` field) and otherwise giving up cleanly. The verdict stays native and is never produced by an LLM; only the proposal is generative. The loop is backstopped by the same `CapExceeded`/wall-budget protection as every node ([runner.py:46](agents/hyperion/src/hyperion/crews/runner.py#L46)).
   - `bank` → Phase 2 `store_lemma`.
4. **`config/workflows/lean-prove.json`** (templated on [parallel-research.json](agents/hyperion/config/workflows/parallel-research.json), which already proves the fan-out/fan-in we need): `decompose → skeleton_check → (retrieve ‖ synthesize) → verify → bank`. The two sourcing nodes share `upstream: ["<sub-goal>"]` so `_wave_groups` ([runner.py:587](agents/hyperion/src/hyperion/crews/runner.py#L587)) runs them concurrently — **this is Path A ‖ Path B for free.**
5. **Per-sub-goal fan-out:** one subworkflow instance per `sorry`, reusing the existing `subworkflow` node kind and `_run_subworkflow` ([runner.py:665](agents/hyperion/src/hyperion/crews/runner.py#L665)) — or a loop over `plan.active_subtasks()` ([plan_contract.py:113](agents/hyperion/src/hyperion/crews/plan_contract.py#L113)). Prefer subworkflow: it's tested and gives per-sub-goal isolation.

### Test Gate — Phase 4

- **Files:** `tests/test_lean_prove_workflow.py` (orchestration, the centerpiece), `tests/test_plan_contract_lean.py` (unit).
- **Under test (mock LLM + `_mock_lean`, exactly the [test_subworkflow.py](agents/hyperion/tests/test_subworkflow.py) pattern):**
  - Plan-contract parser reads `lean_type`/`scaffold` and **still tolerates** old/partial plans (no raise).
  - `retrieve` and `synthesize` land in the **same wave** (assert concurrency via the wave grouping) and the `verify` node waits for both.
  - **Repair is delegated, the verdict is not:** the `verify` handler invokes the `repair` agent for each proposal (assert the agent is called), but a mocked `repair` that returns a still-broken candidate **never** yields a pass — only `verify_lean` decides `ok` (proves the oracle can't be faked, §1a invariant).
  - **Repair loop terminates:** a `repair` agent that never converges hits `cap_repair_iters` and fails the sub-goal cleanly instead of spinning (assert the cap fires).
  - BOTH-FAIL → Path A advances to next-best; one-passes → sub-goal discharged; `bank` is called with the winner.
  - A full mocked run over a 2-`sorry` scaffold produces `artifacts/result.lean` with no `sorry`.
- **Definition of Done:**
  - [ ] End-to-end mocked run returns `status: done` with a sorry-free `result.lean`.
  - [ ] Wave concurrency of retrieve‖synthesize asserted.
  - [ ] `verify` delegates proposals to the `repair` agent, but only `verify_lean` yields a pass (oracle-not-faked invariant proven by test).
  - [ ] Non-converging repair provably aborts at `cap_repair_iters`.
  - [ ] Plan-contract changes don't break `test_*` for the existing planner; full suite green.
  - [ ] *(Integration, `@pytest.mark.lean`)* at least one **real** trivial theorem proved end-to-end through the workflow against the live sidecar.

---

## Phase 5 — Compare, triple-log, and the anti-unification abstractor

**Goal:** the measurement instrument (compare + triple log) and the **one genuinely novel module** (abstraction). Built last because it's most likely to need iteration and everything beneath it must be solid (baseline §6.5–6.6).

### Deliverables

1. **Compare step** (`compare` native handler) — deterministic: given two verified lemmas, prefer the **more general / shorter / more-reusable** one. Pure function, heavily unit-tested. (Baseline §3 "build new".)
2. **Triple logging** — log the `(retrieved, synthesized, winner)` triple to the blackboard via `context_put` ([context_store.py:74](agents/hyperion/src/hyperion/memory/context_store.py#L74)) **and** the bank. **This triple IS the thesis dataset** — the preference signal and the experiment's core measurement. Treat its schema as a first-class artifact.
3. **Anti-unification abstractor** (`abstractor` agent + `verify_lean` re-check) — the novel contribution, no repo analog:
   - Fires whenever Path B produced a fresh verified lemma (even if Path A also closed the goal — **anti-starvation**).
   - Lift concrete constants/types → variables; keep the **most-general form that still type-checks**; **re-verify** the abstracted lemma via Phase 1's `verify_lean` (`full` mode).
   - Wire as a node downstream of `verify`, upstream of `bank`, so the bank stores the *abstracted* lemma.
4. **Workflow update:** insert `compare` and `abstract` into `lean-prove.json` between `verify` and `bank`.

### Test Gate — Phase 5

- **Files:** `tests/test_compare.py` (unit), `tests/test_abstractor.py` (unit + integration), update `tests/test_lean_prove_workflow.py`.
- **Under test:**
  - *Compare:* given two verified lemmas, the more general/shorter wins; ties broken deterministically; the triple is logged with the agreed schema.
  - *Abstractor:* a lemma with a liftable constant abstracts to a more general statement that **re-verifies** (`_mock_lean` for unit; **real Lean** for integration — an over-abstraction that no longer type-checks must be **rejected**, falling back to the most-general form that did).
  - *Anti-starvation:* abstraction fires on a fresh Path-B lemma even when Path A also closed the goal (assert the node ran).
  - *Workflow:* `abstract` runs after `verify`, before `bank`; the bank receives the abstracted form.
- **Definition of Done:**
  - [ ] Compare is a pure, fully-unit-tested function; triple-log schema fixed and asserted.
  - [ ] *(Integration)* a real lemma is abstracted, **re-verified against live Lean**, and an over-abstraction is correctly rejected.
  - [ ] Abstraction fires under the anti-starvation condition.
  - [ ] Full suite green; the system proves a real multi-`sorry` theorem end-to-end with abstraction on.

---

## Post-work — Measurement, policy knob, and hardening

**Goal:** turn the working system into a measurable experiment and a deployable service. (Baseline §5 policy knob, risks #3/#4.)

1. **Thesis-curve harness.** Plot **solved-rate** and **compute-per-theorem** vs. **bank size**, on **unseen goals**. The Phase-5 triple log is the dataset; this harness is the read-out. The claim: synthesizer win-rate **falls** as the bank fills ⇒ reuse transfers ⇒ the snowball is real. Build this as a script over run history + the triple log, not in the hot path.
2. **RESEARCH/DEPLOY policy knob** — node `when` conditions ([workflows.py:50](agents/hyperion/src/hyperion/crews/workflows.py#L50), `NodeWhen`) + a `settings` flag:
   - **RESEARCH:** `synthesize` fires on *every* sub-goal (A+B always). The comparison *is* the experiment; keeps the bank growing.
   - **DEPLOY:** gate `synthesize` (`when`: low-retrieval-confidence / novel-goal); else greedy-retrieval. Cheap once the bank is mature.
   The `when` mechanism already exists and is honored by `_node_fires` ([runner.py:540](agents/hyperion/src/hyperion/crews/runner.py#L540)) — this is configuration, not code.
3. **Re-tune the caps (baseline risk #3 — the cost model is inverted).** Hyperion assumed the LLM was the expensive step and the oracle was cheap; we run **many** cheap verifications and the LLM is the cost. Revisit `cap_tool_loop`, `cap_wall_seconds`, **`cap_repair_iters`** (the §1a repair-loop budget — each iteration is one `repair`-agent LLM call plus a cheap verify, so this directly trades proof quality against LLM spend), and the per-stage budgets in [config.py:144-154](agents/hyperion/src/hyperion/config.py#L144-L154) against the **real** sidecar latency recorded in Phase 1's integration test.
4. **Bank made load-bearing** — finish the Phase-2 decision: loud write failures, surfaced to the run result, with retry. A lost lemma stalls the snowball.
5. **Observability for free** — the MCP/HTTP/webhook/run-history surface ([server/](agents/hyperion/src/hyperion/server/)) already streams progress and persists runs. Confirm the prover's native nodes emit progress callbacks so the existing trace UI attributes them.

### Test Gate — Post-work

- **Definition of Done:**
  - [ ] Thesis-curve harness produces a plot from a real multi-theorem run; numbers are reproducible from the triple log.
  - [ ] Flipping the RESEARCH/DEPLOY flag measurably changes whether `synthesize` fires (asserted by a routing test, like the `skipped` records in `_node_fires`).
  - [ ] Caps re-tuned against measured sidecar latency; documented.
  - [ ] A killed bank write fails loudly in an integration test.

---

## Consolidated test matrix (the "test each component" contract, in one place)

| Component (build phase) | Test file | Tier(s) | The one assertion that proves it works |
|---|---|---|---|
| Lean verifier (P1) | `test_lean_verify.py` | unit + lean | Real true theorem `ok=True`; infra-down ≠ false `ok=False` |
| Native node seam (P1) | `test_native_node.py` | orchestration | A `native` node runs in the DAG; existing dispatch unchanged |
| Lemma bank (P2) | `test_lemma_bank.py` | unit | Same lemma stored twice → one point; write failure observable |
| Applicability retrieval (P3) | `test_lemma_retrieval.py` | unit | Textually-similar non-applying lemma is dropped; applying one kept |
| Prover workflow (P4) | `test_lean_prove_workflow.py` | orchestration + lean | Mocked 2-`sorry` run → sorry-free `result.lean`; repair aborts at `cap_repair_iters` |
| Verify controller + repair (P4) | `test_lean_prove_workflow.py` | orchestration | `repair` agent is called per proposal, but only `verify_lean` yields a pass (oracle-not-faked) |
| Plan contract (P4) | `test_plan_contract_lean.py` | unit | Reads `lean_type`/`scaffold`; still tolerates old plans |
| Compare (P5) | `test_compare.py` | unit | More-general lemma wins; triple logged to schema |
| Abstractor (P5) | `test_abstractor.py` | unit + lean | Abstraction re-verifies; over-abstraction rejected |
| Policy knob (Post) | extend workflow test | orchestration | DEPLOY flag suppresses `synthesize` firing |

Run discipline: `uv run pytest` (offline tiers) on every commit; `uv run pytest -m lean` (integration) before merging a phase and nightly.

---

## Risk register (carried from baseline §4 + build-specific)

| # | Risk | Where it bites | Mitigation in this plan |
|---|---|---|---|
| R1 | CrewAI is heavy for a verifier-in-the-loop | Phase 4 verify/repair | `verify` is a native *controller* (§1a): native verdict + deterministic routing + native loop, with only the repair *proposal* delegated to a thin `repair` agent. LLM judgment is kept (error-reading), but never owns the verdict or the loop |
| R2 | Textual relevance ≠ applicability | Phase 3 | Applicability gate (`apply`/`exact` probe) after rerank; tested explicitly |
| R3 | Cost model inverted (oracle cheap, not expensive) | Post-work caps | Re-tune caps against measured sidecar latency from P1 integration test |
| R4 | Episodic store swallows errors | Phase 2 / Post-work | Bank write path made loud & load-bearing; proven by test |
| R5 | Local-first, personalized assumptions | Pre-work | De-personalize step; config-driven collection names |
| R6 | Provenance / license | Pre-work step 1 | **Blocking** confirmation before any build |
| B1 | Lean toolchain is the long pole | Phase 1 | Built first; warm-cache sidecar; its own integration gate |
| B2 | Mathlib cold-build wall-time | Phase 1 sidecar | Warm cache baked into image; latency measured and budgeted |
| B3 | Abstractor over-generalizes past type-checking | Phase 5 | Re-verify in Lean; reject and fall back to most-general type-checking form |
| B4 | Breaking the orchestration net | Every phase | Additive changes only; full suite green at every commit |

---

## System-level Definition of Done

The transformation is complete when:

- [ ] All nine Test Gates above are green (offline tiers in CI, `lean` tier nightly).
- [ ] The system proves a **real, unseen, multi-`sorry` theorem** end-to-end against the live Lean sidecar, with abstraction on, producing a `sorry`-free `result.lean`.
- [ ] The thesis curve is plottable from the triple log and shows synthesizer win-rate declining as the bank grows.
- [ ] The RESEARCH/DEPLOY knob flips behavior via config alone.
- [ ] The original 15 orchestration test files still pass unmodified.

**One-line verdict (unchanged from baseline §7):** reuse the orchestration spine, spend the effort on the oracle, the lemma bank, and the abstractor — and prove each one works the moment it's built.
