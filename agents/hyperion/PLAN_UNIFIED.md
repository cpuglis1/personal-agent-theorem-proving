# Project Hyperion — Unified Implementation Plan

**Status:** Drafted 2026-05-29. **Supersedes** `archive/PLAN_UPGRADES.superseded.md` and
`archive/hyperion-ui-PLAN.superseded.md` (both archived under `agents/hyperion/archive/`) —
this document merges both into one conflict-free sequence.
**Owner:** Charlie Tolleson.
**Depends on:** `agents/hyperion/` Phase 0+1 (the original `PLAN.md`) — complete and running in
Docker as of 2026-05-29.

This plan combines two previously-separate efforts:

- **Track A — Advanced agent upgrades** (was `archive/PLAN_UPGRADES.superseded.md`): pausable crew, human-in-the-loop,
  shared context layer, generative-UI affordances, orchestration/webhooks.
- **Track B — Agent Manager UI** (was `archive/hyperion-ui-PLAN.superseded.md`): data-driven agents, dynamic routing,
  a React management UI, thresholds/metrics, scheduler.

They overlapped hard on three files (`crews/default.py`, `server/api.py`, the planner's `plan.md`
contract) and on one architectural axis (agents-as-code vs agents-as-data). Run in parallel they
would conflict. **This plan reworks them into one sequence that builds each shared foundation
exactly once.**

---

## 1. The meshing strategy (read this first — it is what makes the plan conflict-free)

Both tracks needed to rewrite the same three things. Instead of rewriting twice, we **build a single
substrate, then layer every feature on top of it as additive hooks.** Four seams carry all the load:

1. **The agent record + tool registry** (Track B's idea). Agents become JSON data, tools become a
   named registry. *Every* Track-A capability that adds an agent behavior or tool (recall, context,
   feedback, affordance-ask, critic) plugs in here as a registry entry or a record edit — never as a
   code edit to a hardcoded factory.

2. **The unified stage-runner** (replaces both tracks' `build_crew` rewrites with one). It is
   simultaneously data-driven (Track B), stage-based `plan→work→synthesize` (Track B), DAG-routed
   (Track B), and **resumable between stages** (Track A's pausable crew). It is rewritten **once** in
   Phase 1 with **stubbed hook points** (`route()`, `gate()`, `inject_feedback()`, `discover_context()`)
   that later phases fill in. No later phase restructures the runner.

3. **Locked API + data contracts, defined up front in Phase 0.** `TaskRequest`, `TaskResponse`, the
   `tasks` table, the `plan.md` frontmatter, the agent record, and the affordance schema are defined
   in full immediately — with fields that stay `null`/unused until the phase that populates them.
   Later phases **populate fields and add endpoints; they never change a field's shape.** This is why
   the UI (built mid-plan) never needs reworking when later backend phases land.

4. **One planner output contract.** `plan.md` frontmatter carries Track B's routing signals
   (`task_type`, `keywords`) *and* Track A's HITL/context signals (`options[]`, `context_brief`,
   `needs_review`) in a single schema. The planner record's prompt is edited to emit more of it over
   time; the parser is written once.

**The rule for every phase after Phase 1:** you may add a registry entry, fill a stubbed hook, populate
a pre-defined field, or add a new endpoint/file. You may **not** rewrite the runner, restructure a
response model, or change an existing field's meaning. If a phase seems to require that, the contract
in Phase 0 was wrong — fix Phase 0, don't fork the structure.

---

## 2. Goals & non-goals (merged)

**Goals**
- A self-hosted multi-agent system that plans, researches, optionally executes code, and synthesizes —
  reachable from OWUI chat, Claude Code (MCP + `/hyperion`), **and** a dedicated web UI.
- **Agents are data**, not code: editable records with prompts, model, tools, triggers, thresholds.
- **Dynamic routing**: agents activate per-task by rule (keyword / task_type / upstream / schedule),
  not a fixed roster.
- **Human-in-the-loop**: approve plans, steer mid-run, get deviation alerts — without polling babysitting.
- **Context-aware**: a shared, prioritized, auto-populated context layer (extends episodic memory).
- **Generative-UI affordances**: agents emit clarifying questions / choice cards / forms, rendered by
  *all three* surfaces (OWUI, Claude Code, the web UI).
- **Locally orchestratable**: standardized agent-card + webhooks/cron so other systems can drive it.
- Single LLM gateway (LiteLLM `:4000`); self-hosted search/rerank/tracing; Qdrant for RAG + memory.

**Non-goals (v1)**
- Multi-user auth / RBAC. Single-user, localhost only. (Track B's "User Management" dropped.)
- Internet exposure. Everything binds to localhost / `ai-net`.
- Rebuilding tracing — Langfuse owns deep observability; surfaces link out.
- **Note:** the original `PLAN.md` §9 listed "a web UI of its own" as out of scope. That is
  **deliberately reversed here** (Track B). Track A's earlier "no UI, affordances only" framing is
  superseded: affordances now render in OWUI, Claude Code, *and* the web UI.

---

## 3. Decisions (merged)

| Question | Decision | Rationale |
|---|---|---|
| Agents: code or data? | **Data** — JSON records under `config/agents/<id>.json`, git-tracked | Human-diffable; git is free version history; survives rebuilds via volume mount; the seam every other feature plugs into |
| Crew execution | **One unified stage-runner**, resumable between stages | Collapses Track A's pausable rewrite and Track B's data-driven/DAG rewrite into a single engine |
| Routing | **Rule-based** (keyword/task_type/upstream/schedule) + planner-derived `task_type` | Deterministic, testable, no extra LLM cost; avoids CrewAI's unpredictable internal delegation |
| HITL gating | **Between stages**, not inside a crew | CrewAI can't safely interrupt mid-`kickoff`; gating between staged crews is clean and restart-survivable |
| Tooling | **Named tool registry**; agents reference tools by name | Lets data-driven agents (and the UI) grant/revoke capabilities; the integration point for all new Track-A tools |
| Context | Task-scoped blackboard (`context.json`) + durable Qdrant `hyperion_memory` | Generalizes the already-built episodic memory; `recall_similar_tasks` becomes a registry tool |
| UI stack | React + Vite + TS + Tailwind, served by nginx in `hyperion-ui` container `:4102` | Track B pick; SPA, all state in the Hyperion API |
| Orchestration | A2A-style `/.well-known/agent.json` + local webhooks/cron | Drops Track A's Salesforce framing; keeps standardized API + triggers |
| Critic | An agent **record** (`active:false`), opt-in via trigger/flag | Unifies `PLAN.md` §2.5 critic with the data-driven model |

---

## 4. Locked contracts (Phase 0 deliverable — defined once, populated later)

These are written in full before any feature work. Fields marked *(filled: Phase N)* exist from day one
but stay `null`/default until phase N populates them.

### 4.1 Agent record — `config/agents/<id>.json`

```jsonc
{
  "id": "planner",                 // stable slug; filename = <id>.json
  "name": "Task Planner",
  "description": "Decomposes requests into a structured plan.",
  "group": "core",                 // dashboard grouping
  "active": true,
  "stage": "plan",                 // "plan" | "work" | "synthesize"
  "role": "Task Planner",          // CrewAI Agent(role=)
  "goal": "...",
  "backstory": "...",
  "model_alias": "smart",          // LiteLLM alias or pinned model
  "fallback_alias": null,
  "temperature": 0.1,
  "top_p": null, "max_tokens": null, "max_iter": 3,
  "tools": ["workspace_write"],    // names resolved against TOOL_REGISTRY
  "trigger": { "type": "always" }, // always|keyword|task_type|upstream|schedule (filled: Phase 2)
  "order": 1,
  "thresholds": { "max_input_tokens": null, "max_output_tokens": null, "max_activations_per_day": null }
}
```
Seed planner/researcher/synthesizer from the current factory literals **byte-identically**; seed
`developer` and `critic` as `active:false`.

### 4.2 `plan.md` YAML frontmatter — the single planner contract

```yaml
task_type: research | code | mixed     # (filled: Phase 2) routing signal
keywords: [ ... ]                       # (filled: Phase 2) routing signal
context_brief: |                        # (filled: Phase 4) auto-discovered context
  ...
needs_review: true | false              # (existing) critic trigger
options:                                # (filled: Phase 3) proactive-planning alternatives
  - id: a
    summary: "..."
    subtasks: [ { id: s1, description: "..." } ]
    est_tool_calls: 6
selected_option: null                   # (filled: Phase 3) set by orchestrator (auto) or human (approve)
```
**Semantics:** when HITL is off, the orchestrator auto-sets `selected_option` to the first option
(planner may emit just one). The router (Phase 2) evaluates work-stage triggers against the *selected*
option's `subtasks` + `keywords`. Missing `task_type` is treated as `mixed` (graceful default).

### 4.3 API request/response — `server/api.py`

```python
class TaskRequest(BaseModel):
    task: str
    schema_version: int = 1                       # (Phase 9) external-caller contract
    hitl: Literal["off","plan","full"] = "off"    # (filled: Phase 3)
    critic: bool = False                          # (existing)
    callback_url: str | None = None               # (filled: Phase 9)
    cap_wall_seconds: int | None = None
    cap_input_tokens: int | None = None
    cap_output_tokens: int | None = None

class TaskResponse(BaseModel):
    task_id: str
    status: str            # queued|running|awaiting_approval|awaiting_input|done|failed (states: Phase 3)
    error: str | None = None
    result_path: str | None = None
    progress_lines: list[str] = []
    routing: dict | None = None             # (filled: Phase 2) {selected_agents, skipped, dag}
    pending_stage: str | None = None        # (filled: Phase 3)
    pending_affordance: dict | None = None  # (filled: Phase 6)
```

### 4.4 `tasks` table (sqlite) — all columns added in Phase 0

`task_id, status, request, error, result_path, created_at, updated_at` (existing)
`+ hitl TEXT, pending_stage TEXT, pending_payload TEXT, resume_token TEXT, routing TEXT` (new, nullable).

### 4.5 Affordance schema — `server/affordances.py`

```jsonc
{ "type": "choice|form|question|confirm", "prompt": "...", "options": [...], "fields": [...] }
```
Answers route through `/approve` (choices) or `/feedback` (free-text) — Phase 6 reuses Phase 3 plumbing.

### 4.6 Tool registry — `agents/registry.py`

`TOOL_REGISTRY: dict[str, Callable[[str], BaseTool]]`. Seeded with the existing tools
(`workspace_read/write`, `web_search`, `second_brain`, `reranker`). Later phases register:
`recall_similar_tasks` (Phase 4), `context_put/get` (Phase 4), `read_human_feedback` (Phase 6),
`ask_user` (Phase 6), and the backend Phase-2 tools (`code_runner`, `notion_write`).

---

## 5. Architecture (unified)

```
Surfaces:   OWUI tool        Claude Code (MCP + /hyperion)        hyperion-ui (React :4102)
                \                     |                                   /
                 \____________________|__________________________________/
                                      v
                       Hyperion API (FastAPI :4100) + MCP (:4101)
                                      |
                         ┌────────────┴─────────────┐
                         |   Unified stage-runner    |   crews/runner.py
                         |   plan → work → synth      |   - data-driven (records)
                         |   resumable between stages |   - DAG-routed work stage
                         └─┬────────┬─────────┬──────┘   - gate/feedback/context hooks
                router.py ─┘        |         └─ registry.py (agents + TOOL_REGISTRY)
                                    v
        LiteLLM :4000   |   Tools   |   Qdrant: second_brain + hyperion_memory   |   Langfuse :3001
                            SearXNG / Infinity / (code_runner DinD)
```

New containers vs today: `hyperion-ui` (`:4102`). Config dir `agents/hyperion/config` mounted into
`hyperion` and `hyperion-mcp` (`HYPERION_CONFIG_DIR=/app/config`). (SearXNG/Infinity/Langfuse already exist.)

---

## 6. Phased implementation

Each phase ends with an acceptance check. **Do not advance until it passes.** The "touches" line names
every shared file the phase modifies and confirms the modification is additive.

### Phase 0 — Contracts & substrate *(no behavior change)*
- Implement §4 in full: `registry.py` (`AgentRecord` pydantic model, `load/save/delete/validate`,
  `TOOL_REGISTRY` seeded with existing tools), seed `config/agents/*.json` from current factory literals,
  extend `TaskRequest`/`TaskResponse` and the `tasks` table with all future-nullable fields,
  `server/affordances.py` schema, `crews/runner.py` skeleton with **stubbed no-op hooks** `route()`,
  `gate()`, `inject_feedback()`, `discover_context()`.
- Docker: mount config dir into both backend containers; add `HYPERION_CONFIG_DIR`.
- **Touches:** `server/api.py` (additive fields only), `tasks` schema (additive columns), compose (new mount/env). New files otherwise.
- **Acceptance:** package imports; seeds validate; existing crew still runs unchanged (runner not yet wired in); `GET /tasks/{id}` returns the new nullable fields as `null`.

### Phase 1 — Unified stage-runner *(the one and only execution rewrite)*
- `crews/runner.py`: `run_task(task_id, request, hitl, caps...)` executes **stage by stage** —
  build a `Crew` of just the `plan` agents, kickoff, persist `plan.md`; then `work` agents; then
  `synthesize`. Reuse existing `step_callback`/`task_callback` progress wiring and the `result.md`
  fallback-write. Agents are constructed **from records** using the *exact* CrewAI 0.86 kwargs the
  current factories use (no new kwargs — pin breaks between minors, see `CLAUDE.md`).
- For this phase the hooks are no-ops: `route()` returns all active agents in stage order (work = the
  seeded researcher), `gate()` never pauses, `discover_context()`/`inject_feedback()` do nothing.
- `crews/default.py` becomes a thin shim delegating to `runner.run_task` (or is replaced and callers updated).
- **Touches:** `crews/default.py` (rewritten **once**, here), `server/api.py` (point `_run_and_update` at the runner). New `crews/runner.py`.
- **Acceptance:** a run with the seeded store produces output **byte-identical** to today's fixed pipeline; editing a `backstory` in JSON and rerunning reflects the change; the runner demonstrably executes three discrete stages internally (log shows stage boundaries) even though it doesn't pause.

### Phase 2 — Routing engine *(fills the `route()` hook)*
- `crews/router.py`: evaluate triggers against `(request, task_type, selected-option keywords/subtasks)`,
  build the work-stage DAG, topo-sort by `upstream` edges then `order`, reject cycles. Wire it into
  the runner's `route()` hook.
- Edit the **planner record's prompt** to emit `task_type` + `keywords` in `plan.md` frontmatter; write
  the frontmatter parser (used by both router and later HITL).
- Populate `TaskResponse.routing` = `{selected_agents, skipped:[{id,reason}], dag}`.
- **Touches:** `crews/runner.py` (fill `route()` stub — additive), planner JSON record (prompt edit), `server/api.py` (populate `routing` field). New `crews/router.py`.
- **Acceptance:** a work agent with `trigger:keyword:[code]` fires only when the request/plan contains "code"; an `upstream` agent runs after its dep; a cycle is rejected; a planner-only task (no work match) still produces a result; `routing` explains who fired and who was skipped.

### Phase 3 — Pausable execution + HITL plan gate *(fills the `gate()` hook)*
- Runner gains suspend/resume: after the plan stage, if `hitl in (plan,full)`, persist
  `pending_stage`/`pending_payload`/`resume_token` to sqlite + a `tasks/{id}/.stage` marker, set status
  `awaiting_approval`, and **return** (background coroutine exits, not blocks). `resume_task()` re-enters
  from the marker by rebuilding the next stage's crew from on-disk artifacts.
- Edit the **planner record's prompt** to emit `options[]` (2–3 alternative paths). Orchestrator
  auto-selects `options[0]` when HITL off.
- `POST /tasks/{id}/approve` `{action:"approve"|"revise"|"reject", chosen_option?, edits?}`. `revise`
  re-runs the planner with `edits` as context (max 2 passes). Router (Phase 2) re-evaluates against the
  chosen option.
- **State durability:** persist `pending_affordance`/feedback to workspace+sqlite and rehydrate
  `_PROGRESS` on startup (HITL makes the previously-cosmetic in-memory loss a correctness bug).
- **Touches:** `crews/runner.py` (fill `gate()` stub + resume — additive), planner record (prompt), `server/api.py` (new `/approve` endpoint + populate `pending_stage`; states already defined in Phase 0), `tasks` table (columns already exist). New states use existing columns.
- **Acceptance:** `hitl:"plan"` halts at `awaiting_approval` exposing ≥2 options and the coroutine has returned; `approve {chosen_option:"b"}` resumes and builds `result.md` from option B; killing+restarting the API mid-wait preserves the pending state and still resumes.

### Phase 4 — Context layer *(fills `discover_context()`; adds registry tools)*
- `memory/context_store.py`: task-scoped `tasks/{id}/context.json` blackboard with tools
  `context_put/get`; register `recall_similar_tasks` (already implemented in `memory/episodic.py`,
  currently unused) as a registry tool and grant it to the planner record.
- Extend `tools/reranker.py` into a shared `prioritize(query, candidates)` used by all retrieval paths;
  enforce a per-stage **input-token budget** (this finally implements the deferred input cap from
  `PLAN.md` §2.4 / `crews/default.py:6` TODO) by trimming lowest-scored context to fit.
- Fill `discover_context()`: a cheap (`model_cheap`) pre-plan pass writes `context_brief` into `plan.md`.
- **Touches:** `crews/runner.py` (fill `discover_context()` — additive), `tools/reranker.py` (extend), `registry.py` (register tools — additive), planner record (grant tool). New `memory/context_store.py`.
- **Acceptance:** a duplicate request causes the plan to cite a prior task; synthesizer reads a `context.json` fact it didn't produce; a task whose raw retrieval exceeds the input cap now **completes** (trimmed-to-budget, logged) instead of aborting.

### Phase 5 — Agent CRUD + options API *(additive endpoints only)*
- `/agents` CRUD + `/agents/{id}/duplicate`; `/tools` (from registry); `/models` (LiteLLM `/models` +
  aliases); `PUT /config` (global primary/fallback model, no restart). Server-side validation:
  model/tool names must exist; slug-shaped unique id; trigger DAG acyclic; **at least one active `plan`
  and one active `synthesize` agent must remain** after any mutation.
- **Touches:** `server/api.py` (new endpoints), `registry.py` (already has save/validate). No model restructuring.
- **Acceptance:** create/edit/delete/duplicate via `curl`; invalid model/tool/cycle and last-plan/synth deletion are rejected with clear messages; a new agent participates in the next run per its trigger.

### Phase 6 — Feedback, alerts, affordances *(fills `inject_feedback()`; reuses Phase 3 plumbing)*
- `POST /tasks/{id}/feedback {message}` → append `tasks/{id}/feedback.md` + per-task queue; runner
  drains it between subtasks via the `inject_feedback()` hook; tool `read_human_feedback()` (registry).
- Deviation alerts: flag soft thresholds (tool-loop ≥ cap−1, elapsed > 0.7× wall, empty-artifact stage);
  emit one alert via `PushNotification`/`alerts.md`. `HYPERION_HITL_ALERTS=on|off`.
- Affordances: agents emit via `ask_user()` (registry) → `tasks/{id}/affordances.jsonl`; populate
  `TaskResponse.pending_affordance`; answers route through `/approve` (choice) or `/feedback` (text).
- MCP tools `hyperion_approve`/`hyperion_feedback`; OWUI sibling tool that renders affordances as
  numbered text and maps replies back.
- **Touches:** `crews/runner.py` (fill `inject_feedback()` — additive), `server/api.py` (new `/feedback`; populate `pending_affordance`), `server/mcp.py` (new tools), `registry.py` (register tools), OWUI tool, `skills/commands/hyperion.md`. New `server/affordances.py` already stubbed in Phase 0.
- **Acceptance:** a running `hitl:"full"` task changes behavior after `/feedback`; an under-specified request makes the planner emit a `question` affordance instead of guessing, and answering it resumes; a near-cap loop emits exactly one alert and continues.

### Phase 7 — Web UI *(separate directory — no backend file conflict)*
- React app in `agents/hyperion-ui/` (Vite/TS/Tailwind, TanStack Query), nginx-served container `:4102`,
  `VITE_HYPERION_API=http://localhost:4100` (browser → host, **not** `http://hyperion:4100`).
- Pages: **Dashboard** (agent cards by group, status/stage/model/trigger, metrics); **Agent editor**
  (identity/prompt/model/tools/triggers/thresholds, cycle validation, warn on editing core-agent prompts
  that other agents depend on); **Settings** (global model + caps); **Run detail** rendering the Phase-2
  `routing` explanation, Phase-3 **HITL approval buttons**, and Phase-6 **affordance forms** — the
  affordance contract now has a *third* renderer.
- **Touches:** new `agents/hyperion-ui/` only; new `hyperion-ui` compose service. Zero backend code changes (API already complete and stable from Phases 0–6).
- **Acceptance:** full agent lifecycle in the browser (add → edit → activate → run → see routing/result);
  approve a `hitl:"plan"` run from the UI; answer an affordance from the UI.

### Phase 8 — Thresholds, metrics, monitoring, scheduler
- `GET/PUT /thresholds` (token/wall caps + per-agent overrides + LiteLLM key budget/rpm via admin API);
  `GET /metrics` (per-agent activations/error-rate/usage-vs-cap from `state.db` + LiteLLM `/spend` +
  Langfuse); `GET /tasks` paginated list. Monitoring page: runs table, per-agent tiles, **Langfuse
  deep-links**. `scheduler.py`: single `AsyncIOScheduler` started in FastAPI `startup`, fires
  `schedule`-trigger agents as entry-point tasks.
- **Touches:** `server/api.py` (new endpoints), new `scheduler.py`, UI monitoring page. Additive.
- **Acceptance:** a per-agent token cap makes a run abort with `CapExceeded`; usage bars reflect real
  activity; Langfuse links resolve to the right session; a `cron:"*/5 * * * *"` agent enqueues every 5 min.

### Phase 9 — Orchestration & polish
- `GET /.well-known/agent.json` (A2A-style agent card; `schema_version:1` on `POST /tasks`); outbound
  webhooks (`callback_url`, private/loopback host allowlist — SSRF guard); Synthesizer follow-up
  affordances wired to `notion_write`; export/import (config-dir zip); group filters; UI toasts on
  threshold-hit/agent-failure. Documented n8n/cron recipe (config + docs, no new service).
- **Touches:** `server/api.py` (new endpoints + populate `callback_url`/`schema_version`), UI polish. Additive.
- **Acceptance:** `curl /.well-known/agent.json` returns a valid descriptor; `callback_url` fires exactly
  one POST on completion; "save to Notion" follow-up creates a page; export/import round-trips the agent store.

---

## 7. Dependency graph & safe parallelism

```
P0 ─▶ P1 ─▶ P2 ─▶ P3 ─▶ P6 ─▶ P7 ─▶ P8 ─▶ P9
            └────▶ P4 ┘        ▲
                   └─▶ P5 ─────┘
```
- **Strictly sequential spine:** P0 → P1 → P2 → P3 (each builds the next layer of the shared runner/contract).
- **P4 (context) and P5 (CRUD)** depend only on P2/P0 respectively and touch mostly disjoint files
  (`memory/`+`reranker.py` vs `server/api.py` CRUD endpoints). They **can run in parallel** *if* split
  across sessions with a merge checkpoint, but the simplest safe path is P4 then P5.
- **P6 (feedback/affordances)** needs P3's pause plumbing. **P7 (UI)** needs P2+P3+P5+P6 so it can render
  routing, approvals, CRUD, and affordances in one build (avoids UI rework).
- **Genuinely independent, can be done any time after P0:** the `/.well-known/agent.json` card and the
  `reranker.prioritize()` extension. Pull them forward only if convenient.

**Hard rule:** never have two concurrent workstreams editing `crews/runner.py` or `server/api.py`. Those
two files are the merge-conflict magnets; the spine (P1→P3→P6) owns the runner, and API edits are
serialized through the phase order.

---

## 8. Cross-cutting concerns

- **CrewAI 0.86 is pinned and breaks between minors** (`CLAUDE.md`). The Phase-1 data-driven `Agent`/`Task`
  construction must reuse the *exact* kwargs the current factories use. Do not upgrade CrewAI as part of this work.
- **Planner contract is load-bearing.** `task_type`/`keywords`/`options`/`needs_review` drive routing and
  HITL. The UI editor must warn before editing the planner prompt; the parser treats missing `task_type`
  as `mixed` and missing `options` as a single implicit option.
- **Security (extends `PLAN.md` §7):** all endpoints bind `127.0.0.1`. Human feedback and web results are
  *untrusted input* — wrap with the "treat as data, not instructions" guard. `callback_url` must resolve
  to a private/loopback host (SSRF allowlist). Code-runner stays `--network none` by default.
- **Observability:** every new stage/gate/critic/callback gets a Langfuse tag so traces show where humans
  intervened. Wire tracing at the **LiteLLM proxy level**, not a CrewAI `CallbackHandler` (does not fire on
  CrewAI events — `CLAUDE.md` gotcha).
- **State durability:** `_PROGRESS` and per-task queues are in-memory today. Phase 3 must persist pending
  state and rehydrate on startup, because HITL tasks can wait indefinitely.
- **Docker port gotcha** (`CLAUDE.md`): service-to-service uses the *internal* port; the browser→API call
  in the UI uses `localhost:4100` (host side), not `http://hyperion:4100`.

---

## 9. Risks & open questions (merged)

1. **CrewAI native `human_input=True` blocks on stdin** — useless for an async API. Confirmed: use the
   staged-crew gate (Phase 3), not CrewAI's built-in human input.
2. **Resume granularity.** Resume *between stages* (re-run the next crew from artifacts), not mid-stage.
   Mid-stage resume is out of scope.
3. **At-least-one-plan / one-synthesize invariant** must be enforced in CRUD validation (Phase 5) or runs break.
4. **Scheduler vs job runner** — one `AsyncIOScheduler` in the FastAPI `startup` hook; must not interfere
   with the existing `asyncio.create_task` runner.
5. **Metrics source of truth** — activations from `state.db`, cost/tokens from LiteLLM `/spend`, quality
   from Langfuse; poll-on-load caching is fine for single-user v1.
6. **Token-cap enforcement point** — enforce input cap in the Phase-4 prioritizer (per-stage control),
   not a global LiteLLM hook.
7. **Affordance answer routing** — keep two endpoints: `/approve` for plan choices, `/feedback` for free text.

---

## 10. Suggested order

1. **P0 → P1** in full. Verify byte-identical behavior before anything dynamic — this is the foundation
   everything else stands on.
2. **P2 → P3**. Verify routing then pause/approve with `curl` before building UI.
3. **P4** (context, incl. input-cap enforcement), then **P5** (CRUD). Verify with `curl`.
4. **P6** (feedback/alerts/affordances). Verify across MCP + OWUI.
5. **Pause. Demo the backend to Charlie.** (Riskiest changes — runner + HITL — are now validated.)
6. **P7** (UI) against the stabilized, feature-complete API.
7. **P8** (thresholds/metrics/scheduler), then **P9** (orchestration/polish).

Each step ends with its acceptance check. Do not advance until the previous acceptance passes.
