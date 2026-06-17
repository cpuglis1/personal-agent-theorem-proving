# Hyperion as a Baseline for the Lean 4 Theorem-Proving System

**Question:** What in the uploaded `personal-agent` repo can be reused and tuned to host and deploy the parallel-retrieval-plus-synthesis Lean prover described in the workflow doc?

**Short answer:** A lot — more than you'd expect. The repo's `hyperion` agent is not a research toy; it's a self-hosted multi-agent orchestrator whose core abstractions (a topo-sorted DAG runner with parallel waves, conditional node firing, HITL gates, a Qdrant-backed memory layer, a rerank-over-vector retrieval pipeline, circuit breakers, and an MCP front door) line up almost one-to-one with the five phases in your prover doc. You are not building orchestration from scratch. You're swapping the *oracle* (Lean kernel instead of an LLM critic), the *bank contents* (lemmas instead of task episodes), and adding one genuinely new module (anti-unification abstraction). The rest is configuration and tightening.

---

## 1. What the repo actually is

`agents/hyperion` is a Python 3.12 / CrewAi 0.86 / FastAPI service with a sibling MCP server, designed to run on a shared Docker network (`ai-net`) alongside LiteLLM, Qdrant, SearXNG, and an Infinity reranker. The pieces relevant to you:

| Component | File | What it does | Why it matters for the prover |
|---|---|---|---|
| **Workflow DAG engine** | `crews/workflows.py` | Defines workflows as JSON DAGs of nodes; each node binds an agent (or a sub-workflow), declares `upstream` deps, `gate_before`, conditional `when.task_types`. Topo-sorts + validates (cycle/dangling-ref checks). | This *is* your phase graph. Decomposer → Retriever/Synthesizer → Verify → Abstract → Bank becomes a DAG with no new framework. |
| **Parallel-wave runner** | `crews/runner.py` | Executes the topo-sorted DAG, grouping independent nodes into **parallel execution waves** (`_wave_groups`), with HITL `gate()`, plan-revision loops (`_MAX_REVISIONS=2`), and resumability between nodes. | Path A (Retriever) and Path B (Synthesizer) "fire both on the same sub-goal at once" = two nodes in the same wave. Already supported. |
| **Stuck-loop circuit breaker** | `crews/runner.py` → `ToolCallTracker` / `CapExceeded` | Aborts a stage after N identical tool calls (`settings.cap_tool_loop`). | Your Synthesizer Repair Loop needs exactly this guard so an un-converging proof attempt doesn't spin forever. |
| **Vector memory** | `memory/episodic.py` | Stores/recalls records in Qdrant (`hyperion_memory`), deterministic UUID5 upserts, embeddings via the LiteLLM proxy, fault-tolerant. | This is the skeleton of your **lemma bank** — swap the payload schema and the embedding text. |
| **Task blackboard** | `memory/context_store.py` | Per-task `context.json` key/value store all stages read/write, plus CrewAI tool wrappers. | Cross-phase channel for the scaffold, the per-sub-goal goal state, and the `(retrieved, synthesized, winner)` triple. |
| **Rerank-over-vector retrieval** | `tools/second_brain.py` + `tools/reranker.py` | Over-fetch from Qdrant → cross-encoder rerank (Infinity `bge-reranker-v2-m3`) → budget-trim. Fail-soft. | Your Path A retrieval step. Lemma retrieval is the same shape: embed goal → fetch candidates → rerank by applicability. |
| **Structured planner contract** | `crews/plan_contract.py` | `plan.md` with YAML frontmatter: `task_type`, `subtasks`, `options[]`, `selected_option`, `needs_review`. Tolerant parser, never raises. | Your Decomposer's output contract. `subtasks` ≈ the `have ... := sorry` sub-goals; `options` ≈ alternative decompositions for the HITL gate. |
| **MCP front door** | `server/mcp.py` | Exposes `hyperion_run`, `_status`, `_artifact`, `_approve`, `_feedback`. Also accepts a `workflow_prompt` that's **compiled into an ad-hoc DAG**. | Lets you drive the prover from Claude Code / any MCP client, and submit a target theorem the same way you submit a task today. |
| **HTTP API + webhooks + config export** | `server/api.py`, `server/webhooks.py` | Submit/poll/stream, completion webhooks (with SSRF guard), config zip export/import, save-to-Notion. | Deployment surface is done. You get streaming progress and run history for free. |
| **Deploy** | `Dockerfile`, `docker-compose.override.yml`, `pyproject.toml` | `uv`-based image, two services (`hyperion`, `hyperion-mcp`) on `ai-net`, exact-pinned CrewAi. | You reuse the compose topology and add one service (the Lean verifier). |

A `developer` agent already exists (`agents/developer.py`) for "write Python, run it in a sandbox, save artifacts" — but its `tools=[]` is a stubbed placeholder; sandboxed execution was never wired in. **That stub is the natural mounting point for your Lean executor**, except the executor calls `lake`/`lean`, not Python.

---

## 2. The phase-by-phase mapping

Your doc's five phases map onto Hyperion primitives like this:

```
PROVER DOC PHASE                        HYPERION PRIMITIVE
───────────────────────────────────────────────────────────────────────────
INPUT: target theorem            →      task request (POST /tasks or hyperion_run)

PHASE 1  Decompose + scaffold     →     "plan" node (kind=plan).
         (have...:=sorry chain)          Decomposer = a re-prompted planner.
         Lean checks the skeleton  →     A *new* "verify" node downstream of plan,
                                          gate_before optional. Skeleton type-check
                                          replaces today's "needs_review" critic check.
         FAIL → back to Decomposer →     Existing plan-revision loop (_MAX_REVISIONS).

PHASE 2  Parallel lemma sourcing  →     ONE wave with TWO nodes sharing the same
         Path A retrieve (exploit) →      upstream sub-goal node:
         Path B synthesize (explore)→       • retriever node  (kind=work)
                                             • synthesizer node (kind=work)
                                          _wave_groups already runs them concurrently.
         Per-sub-goal fan-out      →     One sub-workflow instance per `sorry`
                                          (subworkflow node), or a loop over the
                                          plan's active_subtasks().

PHASE 3  Verify & compare         →     "verify" node calls the Lean kernel
         (Lean kernel = oracle)          (new tool) instead of an LLM.
         BOTH FAIL → parse errors  →     Retriever: next-best rerank match.
                                          Synthesizer: enters Repair Loop, guarded
                                          by ToolCallTracker/CapExceeded.
         BOTH PASS → COMPARE        →     New compare step: prefer more general /
                                          shorter lemma; log the triple to the
                                          blackboard (context_put) + episodic store.

PHASE 4  Abstract (anti-unify)    →     *** The one genuinely new module. ***
         Lift constants → vars            New "abstractor" agent/tool. No analog
         Re-verify in Lean               in the repo. Re-verify reuses the Phase-3
                                          Lean tool.

PHASE 5  Bank & snowball          →     episodic.py, re-skinned as a lemma bank:
         Embed + store, dedup            • payload: {statement, proof, generality,
         Skip near-duplicates              source_goal, verified_at}
                                          • dedup = score_threshold on upsert
                                          • UUID5 over the normalized statement.

POLICY KNOB (research vs deploy)  →     node `when` conditions + a settings flag.
         research: A+B every goal        Deploy mode: gate the synthesizer node so
         deploy: gate synthesizer        it fires only on low retrieval confidence.
```

The structural fit is strong because both problems are "fan out sub-goals, race two policies, judge with an oracle, persist the winner, repeat." Hyperion already encodes that control flow; the doc just instantiates it for Lean.

---

## 3. What to reuse as-is, tune, or build

### Reuse essentially unchanged
- **The DAG schema and topo-sort/validation** (`workflows.py`). It's clean, well-documented, has cycle and dangling-ref guards, and supports sub-workflow composition. No reason to touch it.
- **The parallel-wave runner loop, gating, resumability, and circuit breaker** (`runner.py`). This is the hardest part to get right and it's already done. You'll edit which *tasks* nodes run, not the orchestration.
- **The rerank-over-vector pipeline** (`reranker.py`, and the over-fetch→rerank→budget-trim pattern in `second_brain.py`). Lemma retrieval is the same shape.
- **The MCP + HTTP + webhook + config-export surface**. Your deployment/observability story is essentially free.
- **The Docker/compose/`uv` topology** on `ai-net`.

### Tune (same module, new content/schema)
- **`memory/episodic.py` → `memory/lemma_bank.py`.** Keep the Qdrant client, the lazy imports, the deterministic-UUID upsert, the fail-soft contract. Change: the embedding text (embed the *lemma statement / goal type*, not request+summary), the payload schema (lemma statement, proof term, generality score, originating sub-goal, verification status), and `recall_similar_tasks → retrieve_lemmas` (rerank by *applicability to the current goal*, which may want a Lean-aware signal, not just cosine).
- **`crews/plan_contract.py`.** Reuse the frontmatter mechanism; change the semantics. `subtasks` → the `have := sorry` sub-goals (you may want to store the Lean type of each, not just a description). `options` → alternative decompositions. Add a field for the scaffold proof text so Phase-1's skeleton check has something to type-check.
- **The agent records** (`config/agents/*.json`, `config/workflows/*.json`). You'll author a `decomposer`, `retriever`, `synthesizer`, `abstractor`, and a `verify` node, plus a `lean-prove` workflow JSON. The existing `planner.json` / `developer.json` are good templates.
- **Model routing** (`llms.py`, `litellm_config.yaml`). Point the synthesizer/decomposer at whatever model you're using for Lean (a math-tuned model behind the proxy). The role-alias + fallback machinery is already there.

### Build new
- **A Lean verification tool.** The single most important new piece. A `tools/lean_verify.py` exposing a CrewAi `BaseTool` (and ideally a plain function the runner can call directly) that:
  - writes a candidate lemma/proof into a Lean project,
  - runs `lake build` / `lean` in a sandbox,
  - returns `{ok: bool, errors: [...], elaborated_term: ...}`.
  This replaces the LLM-critic notion of "verification" with a real oracle. It belongs where `developer.py`'s `tools=[]` stub anticipated a code runner — but it shells out to Lean, and it needs the Lean toolchain in the image (or a sidecar service).
- **The anti-unification / abstraction module** (Phase 4). No analog in the repo. Lift concrete constants and types to variables, keep the most-general form that still type-checks, re-verify. This is your novel contribution and the repo gives you nothing here except a place to hang it (a new agent + the Lean tool for re-verification).
- **The compare step** (Phase 3 "BOTH PASS"). A small deterministic function: given two verified lemmas, pick the more general/shorter/more-reusable one and log the `(retrieved, synthesized, winner)` triple. This is partly orchestration (a node) and partly your measurement instrument — it's the thing that produces the thesis curve.
- **A Lean sidecar service** in compose, or the toolchain baked into the `hyperion` image. Lean builds are heavy; a separate long-lived service with a warm `Mathlib` cache will save you enormous wall-time versus cold-building per sub-goal.

---

## 4. Honest mismatches and risks

A few places where "use it as a baseline" needs a clear-eyed caveat:

1. **CrewAi is a heavy intermediary for a verifier-in-the-loop task.** The repo pins `crewai==0.86.0` with version-sensitive callback workarounds (see the `pyproject.toml` comment and the Phase 3/4 notes). The agents are ReAct loops with `max_iter` budgets — fine for "research and synthesize prose," but for a tight propose→`lean build`→repair cycle you may find CrewAi's abstractions get in the way and want the verify/repair inner loop to be plain Python that the node calls directly, using the framework only for the LLM-generative steps (decompose, synthesize, abstract). The runner already calls plain functions (`gate`, `discover_context`) between agent stages, so this hybrid is natural — don't feel obligated to make the Lean loop "an agent."

2. **Retrieval relevance ≠ applicability.** `bge-reranker-v2-m3` ranks text relevance. A lemma being *textually* similar to a goal doesn't mean it *applies* (unifies with the goal). Expect to add a Lean-aware filter — at minimum, "does `apply`/`exact` succeed" as a cheap post-rerank gate. The reranker stays useful as a coarse pre-filter.

3. **Latency/throughput model is inverted.** Hyperion assumes the LLM call is the expensive step and the "oracle" (critic) is cheap. In your system the Lean kernel is the free, fast oracle and you'll be running *many* verifications. The compose topology is fine, but budget caps (`cap_tool_loop`, stage wall-budgets) were tuned for a different cost profile — revisit them.

4. **The episodic store is best-effort and swallows all errors.** That's correct for "memory is a nice-to-have." For your lemma bank, a *failed bank write* means a verified lemma is lost and the snowball stalls — you may want it louder (the README even notes a similar concern for sub-workflow handoffs). Decide whether the bank is best-effort or load-bearing; it's load-bearing for your thesis.

5. **It's one person's local-first system.** Hardcoded collection names (`second_brain`, `hyperion_memory`), an `importlib` hack to dodge a package-name collision, an author's personal vault assumptions. None are blockers, but you'll be renaming and de-personalizing as you go.

6. **Provenance.** This is someone else's codebase (authored by "Charlie Tolleson" per `pyproject.toml`, MIT-or-whatever license unstated in what I read). Confirm you have the right to fork/derive from it before building commercial work on top, and check `LICENSE`/`.git` history for the actual terms.

---

## 5. Revised workflow (Hyperion-native form)

Below is your doc's pipeline rewritten as it would actually run on this baseline — same five phases, but expressed in the repo's DAG/node/wave/bank vocabulary. The phase boundaries are unchanged; what changes is that each block names the concrete Hyperion mechanism doing the work.

```text
[ INPUT: Formal Target Theorem ]  →  POST /tasks {task: "<theorem>", workflow: "lean-prove"}
        │                             (or hyperion_run via MCP)
        ▼
══════════════════════════════════════════════════════════════════════
 PHASE 1 — DECOMPOSE + SCAFFOLD CHECK
══════════════════════════════════════════════════════════════════════
 NODE decompose  (kind=plan)
   └─ Re-prompted planner. Emits plan.md frontmatter:
        subtasks: [ {id, lean_type} ... ]   # the `have ... := sorry` chain
        options:  [ alternative decompositions ]   # for the HITL gate
        scaffold: "<the have-chain proof text>"
        │
 NODE skeleton_check  (kind=work, upstream=[decompose], gate_before=optional)
   └─ NEW lean_verify tool, "skeleton mode": does the scaffold type-check
      and do the `have`s compose to the target? (sorry always elaborates.)
        ├─ FAIL → runner plan-revision loop (_MAX_REVISIONS) → back to decompose
        └─ PASS → proceed
        ▼
══════════════════════════════════════════════════════════════════════
 PHASE 2 — PARALLEL LEMMA SOURCING        (one sub-workflow per `sorry`)
══════════════════════════════════════════════════════════════════════
 For each sub-goal: a subworkflow whose first wave has TWO nodes
 sharing the sub-goal upstream — _wave_groups runs them concurrently:

   NODE retrieve (Path A, exploit)        NODE synthesize (Path B, explore)
     └─ embed goal → Qdrant lemma_bank       └─ worker_llm writes a bespoke
        → bge reranker → top candidates         lemma for this exact goal
     └─ post-rerank applicability gate          (the real cost; you want it
        (apply/exact succeeds?)                  anyway to grow the bank)
              └───────────────┬────────────────────┘
                              ▼
══════════════════════════════════════════════════════════════════════
 PHASE 3 — VERIFY & COMPARE               (Lean kernel = free oracle)
══════════════════════════════════════════════════════════════════════
 NODE verify  (plain-Python inner loop, NOT a CrewAI agent — see risk #1)
   └─ lean_verify BOTH candidates.
        ├─ BOTH FAIL → parse compiler errors:
        │     ├─ Path A → retriever's next-best rerank match
        │     └─ Path B → Synthesizer Repair Loop, guarded by
        │        ToolCallTracker/CapExceeded → back into PHASE 2
        ├─ ONE PASSES → discharges the sub-goal.
        └─ BOTH PASS → COMPARE (new deterministic step):
              └─ prefer more general / shorter / more-reusable
              └─ context_put + lemma_bank: log (retrieved, synthesized,
                 winner) triple — preference signal AND the experiment's
                 core measurement.
        ▼
══════════════════════════════════════════════════════════════════════
 PHASE 4 — ABSTRACT                        *** new module, no repo analog ***
══════════════════════════════════════════════════════════════════════
 NODE abstract  (new abstractor agent + lean_verify for re-check)
   └─ Whenever Path B produced a fresh verified lemma (even if Path A
      also closed the goal — anti-starvation):
        └─ anti-unification: lift constants/types → variables,
           keep MOST GENERAL form that still type-checks
        └─ re-verify the abstracted lemma via lean_verify
        ▼
══════════════════════════════════════════════════════════════════════
 PHASE 5 — BANK & SNOWBALL
══════════════════════════════════════════════════════════════════════
 NODE bank  (lemma_bank.store, re-skinned episodic.py)
   └─ embed + upsert abstracted lemma (UUID5 over normalized statement)
   └─ dedup: skip near-duplicate (score_threshold on upsert)
   └─ loop to PHASE 2 for the next sub-goal; retrieval hit-rate climbs,
      synthesizer fires less over time.
        ▼
[ OUTPUT: assembled, verified proof — no `sorry` ]  →  artifacts/result.lean
        (poll /tasks/{id}, stream progress, or receive the completion webhook)

──────────────────────────────────────────────────────────────────────
 POLICY KNOB  → node `when` + settings flag
   RESEARCH : synthesize node fires on every sub-goal (A+B always).
              The comparison IS the experiment; keeps the bank growing.
   DEPLOY   : gate the synthesize node (when: low-retrieval-confidence
              or novel-goal); else greedy-retrieval. Cheap once mature.

 THESIS CURVE (unchanged):
   solved-rate & compute-per-theorem vs. bank size, on UNSEEN goals.
   Synthesizer win-rate falling as the bank fills ⇒ reuse transfers
   ⇒ the snowball is real. The compare-step triple log IS this dataset.
```

---

## 6. Suggested build order

A practical sequence that gets you to a measurable v1 fastest, leaning on what's already there:

1. **Fork + de-personalize.** Rename collections, strip the second-brain/Notion/Obsidian assumptions, confirm the license. Get `uv run pytest` green on the untouched orchestration tests first — they're your regression net.
2. **Build `tools/lean_verify.py` + Lean sidecar.** This is the critical path and the longest pole (toolchain in Docker, warm Mathlib cache). Nothing else can be tested end-to-end without it. Get "submit a `.lean` string, get back ok/errors" working standalone before wiring it into a node.
3. **Re-skin `episodic.py` → `lemma_bank.py`** with the new payload + dedup, and adapt the retrieval tool to rerank lemmas with the applicability gate.
4. **Author the agent records + the `lean-prove` workflow JSON.** Decomposer, retriever, synthesizer, verify, bank. Run it *without* abstraction first — a working retrieve-race-verify-bank loop is already a real system.
5. **Add the compare step + triple logging.** The moment this works you can start plotting the thesis curve, even before abstraction.
6. **Add Phase 4 abstraction last.** It's the novel part and the part most likely to need iteration; everything upstream should be solid before you tune it. Measure bank growth and synthesizer win-rate with and without it — that delta is the anti-starvation claim.

---

## 7. One-line verdict

Use it. The repo gives you the entire orchestration spine — DAG runner, parallel waves, gates, circuit breakers, vector memory, rerank retrieval, MCP/HTTP/webhook surface, and Docker topology — which is exactly the scaffolding that's tedious and error-prone to build yourself. You're left to write the Lean verifier (the oracle), re-skin the memory layer into a lemma bank, and build the anti-unification abstractor (your novel module). Budget your effort there, not on plumbing, and revisit the latency/cost caps since your oracle is cheap where Hyperion's was expensive.
