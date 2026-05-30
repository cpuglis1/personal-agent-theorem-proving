# Hyperion Agent Manager UI — Implementation Plan

**Status:** Drafted 2026-05-29 for Sonnet 4.6 to implement.
**Source doc:** "Hyperion Agent Manager UI Design Plan" (vision document, produced by a Hyperion run — see `agents/hyperion/tasks/f6945f67/notes/develop_hyperion_ui_plan.md`).
**Owner:** Charlie Tolleson.
**Depends on:** `agents/hyperion/` (Phase 0+1 complete and running in Docker as of 2026-05-29).

This plan supersedes the vision doc where they conflict. It is grounded in the **actual** Hyperion backend as it exists today, and it expands `agents/hyperion/PLAN.md` §9, which previously listed "a web UI of its own" as out of scope. That decision is **deliberately reversed** here.

---

## 1. Goals & non-goals

**Goals**
- A local web UI to define, customize, and oversee Hyperion agents, running alongside Open WebUI and Langfuse on the existing `ai-net` Docker network.
- Make agents **data-driven**: an agent becomes an editable record, not a hardcoded Python factory.
- Support **dynamic triggers** — agents activate per-task based on rules (keywords, task type, upstream output, schedule), not a fixed sequential roster.
- Per-agent and global usage thresholds; high-level monitoring that **deep-links to Langfuse** for detail.
- Single binary surface for "manage my agent team" that complements (does not replace) OWUI chat and the Claude Code MCP/skill.

**Non-goals (v1)**
- Multi-user auth / RBAC. **Single-user, localhost only** — consistent with `agents/hyperion/PLAN.md` §7. The vision's "User Management & Permissions" is dropped.
- Internet exposure. Everything binds to localhost / Docker network.
- Rebuilding tracing. Langfuse owns deep observability; the UI links out.
- Replacing OWUI or the MCP transport. The UI is a third surface, additive.

**Scope trims from the vision doc (single-user pragmatics)**
| Vision item | Decision |
|---|---|
| User Management & Permissions | **Drop.** Single-user localhost. |
| Version Control for Agent Configs | **Reduce** to: agent records are JSON files under git; "revert" = git restore. No bespoke versioning UI in v1. |
| Export/Import Configs | **Keep, trivial:** zip/JSON dump of the agents config dir. |
| Agent Grouping/Teams | **Keep, light:** a `group` string field + dashboard filter. No coordinated-execution semantics in v1. |

---

## 2. The crux: agents must move from code to data

Everything else depends on this. **Today an agent is a Python function** (`make_planner`/`make_researcher`/`make_synthesizer` in `agents/hyperion/src/hyperion/agents/`), and the crew is a fixed sequential pipeline hardcoded in `crews/default.py`. The UI cannot create or edit agents until an agent is a **record** that the crew builder reads at runtime.

### 2.1 Agent store

**Location:** `agents/hyperion/config/agents/<id>.json` — one file per agent.
**Why JSON files (not a DB table):** human-diffable, git gives free version history (covers the "version control" requirement), export/import is a directory copy, and it survives container rebuilds via a volume mount.

**Mount** the config dir into both `hyperion` and `hyperion-mcp` containers (see §6):
```yaml
- ../agents/hyperion/config:/app/config:rw
```
Override path with `HYPERION_CONFIG_DIR` (default `/app/config` in Docker, `agents/hyperion/config` locally — same `parents[N]` + env-override pattern already used for `tasks_dir`).

### 2.2 Agent schema (`config/agents/<id>.json`)

```jsonc
{
  "id": "planner",                    // stable slug; filename = <id>.json
  "name": "Task Planner",
  "description": "Decomposes requests into a structured plan.",
  "group": "core",                    // free-text, for dashboard grouping
  "active": true,                     // deactivated agents never participate
  "stage": "plan",                    // "plan" | "work" | "synthesize" (see §3)

  // Prompt surface (editable in UI)
  "role": "Task Planner",             // CrewAI Agent(role=)
  "goal": "Decompose the user's request into a clear, actionable plan...",
  "backstory": "You are a seasoned project architect...",

  // Model & behavioral params
  "model_alias": "smart",             // LiteLLM alias or pinned model name
  "fallback_alias": null,             // overrides global fallback for this agent
  "temperature": 0.1,
  "top_p": null,
  "max_tokens": null,
  "max_iter": 3,

  // Capabilities — names resolved against the tool registry (§2.3)
  "tools": ["workspace_write"],

  // Dynamic activation (§3)
  "trigger": { "type": "always" },

  // Ordering within its stage; upstream deps come from trigger.type == "upstream"
  "order": 1,

  // Per-agent thresholds (override global; null = inherit)
  "thresholds": {
    "max_input_tokens": null,
    "max_output_tokens": null,
    "max_activations_per_day": null
  }
}
```

**Seed the store** with the three existing agents (planner/researcher/synthesizer) reproducing their current literals from `agents/hyperion/src/hyperion/agents/*.py` exactly, so day-one behavior is byte-identical. Also seed `developer` and `critic` as `active: false` (they exist in code but aren't in the default crew).

### 2.3 Tool registry

`agents/hyperion/src/hyperion/agents/registry.py`:
- `TOOL_REGISTRY: dict[str, Callable[[str], BaseTool]]` mapping a stable tool name → a factory that takes `task_id` and returns the existing tool instance from `tools/`. Names: `workspace_write`, `workspace_read`, `web_search`, `second_brain`, `reranker`, and (Phase 2 of the backend) `code_runner`, `notion_write`, `recall_similar_tasks`.
- `load_agents() -> list[AgentRecord]`, `load_agent(id)`, `save_agent(record)`, `delete_agent(id)`, `validate(record)`.
- `AgentRecord` is a pydantic model mirroring §2.2 — gives validation for free and is reused by the API layer.

### 2.4 Rewrite `build_crew` to be data-driven

In `crews/default.py` (or a new `crews/dynamic.py` that `default.py` delegates to):
1. Load active agents via the registry.
2. Run the **router** (§3) to select participants and compute the execution DAG.
3. For each selected agent, construct a CrewAI `Agent` from the record (role/goal/backstory/llm/tools/max_iter) using the **same kwargs the current factories use** — CrewAI 0.86 is pinned and breaks between minors (see `CLAUDE.md`), so do not introduce new `Agent`/`Task` kwargs.
4. Build `Task`s with `context=[...]` wired from the DAG edges (upstream dependencies).
5. Keep the existing `step_callback`/`task_callback` progress wiring and the fallback-write of `result.md`.

**Acceptance for the whole §2:** a run with the seeded store produces output identical to today's fixed pipeline, but every agent now loads from JSON. Edit a backstory in the JSON file, rerun, see the change reflected.

---

## 3. Dynamic triggers & routing (in scope for v1)

This is the part the vision explicitly wants and the current backend has none of. Design it as a **rule engine + DAG builder**, not as CrewAI's internal delegation (which is unpredictable across versions).

### 3.1 Stages keep the pipeline coherent

Every agent has a `stage`: `plan` → `work` → `synthesize`. This preserves Hyperion's shape (plan, then do, then write up) while letting the **middle be dynamic**:
- **`plan` stage:** runs first, always. At least one active planner is required (UI enforces this). The planner writes `plan.md` with YAML front-matter that now includes a `task_type` classification (e.g. `research | code | mixed`) and `keywords`. This output is what trigger rules evaluate against.
- **`work` stage:** the dynamic set. Agents here are selected by their `trigger` (§3.2).
- **`synthesize` stage:** runs last; at least one active synthesizer required.

### 3.2 Trigger types

```jsonc
"trigger": { "type": "always" }
"trigger": { "type": "keyword", "any": ["code","script","compute"], "match": "request|plan" }
"trigger": { "type": "task_type", "in": ["code","mixed"] }      // matches planner's task_type
"trigger": { "type": "upstream", "after": "researcher" }        // DAG edge; activate after dep ran
"trigger": { "type": "schedule", "cron": "0 9 * * *", "task": "..." }  // §3.4
```

**Evaluation (`crews/router.py`):**
1. Activate all `stage: plan` + `always` agents → run plan stage.
2. Parse `plan.md` front-matter → `task_type`, `keywords`.
3. For each active `work` agent, evaluate its trigger against `(request, task_type, plan keywords)`. `keyword`/`task_type` → boolean include. `upstream` → include **and** add a DAG edge `dep → agent`.
4. Topologically sort the selected work agents by `upstream` edges, then by `order`. Cycle → reject at save time (§4 validation) and at route time (clear error).
5. Always append `stage: synthesize` agents, depending on all work outputs.
6. If zero work agents match, run plan → synthesize directly (valid; planner-only tasks exist).

`context=[...]` for each CrewAI `Task` is the union of its DAG predecessors, defaulting to the previous stage's outputs — preserving today's `context=[plan_task, research_task]` behavior for the seeded agents.

### 3.3 Trigger evaluation is rule-based, not LLM-based

Keyword/task_type matching is plain Python — no extra LLM cost, deterministic, testable. The only LLM-derived signal is the planner's `task_type`, which it already produces cheaply. This keeps routing fast and debuggable.

### 3.4 Schedule triggers

Time-based triggers run **outside** the per-task router: a background `apscheduler` (AsyncIOScheduler) loop in the API process reads agents with `trigger.type == "schedule"`, and on fire, submits a normal task (`POST /tasks` internally) using `trigger.task` as the request. Acceptance: an agent with `cron: "*/5 * * * *"` enqueues a run every 5 min, visible in the runs table. (Note: schedule-triggered agents are *entry points* that kick off a crew, distinct from work-stage selection.)

### 3.5 Routing transparency

`GET /tasks/{id}` gains a `routing` field: `{ selected_agents: [...], skipped: [{id, reason}], dag: [[from,to],...] }`, surfaced in the UI's run detail so the user can see **why** each agent did/didn't fire. Critical for trusting a dynamic system.

---

## 4. New API endpoints (FastAPI, additive to `server/api.py`)

```
# Agent CRUD
GET    /agents                 → list AgentRecords
POST   /agents                 → create (validates schema, unique id, no trigger cycles)
GET    /agents/{id}
PUT    /agents/{id}            → edit prompts/model/params/tools/trigger/order/active
DELETE /agents/{id}
POST   /agents/{id}/duplicate  → clone with new id

# Registry / options for the editor
GET    /tools                  → [{name, description, requires}] from TOOL_REGISTRY
GET    /models                 → LiteLLM aliases + named models (proxy GET /models) + notes

# Global config
GET    /config   (exists)      → extend: add global primary/fallback model
PUT    /config                 → set global primary/fallback model alias (writes config; no restart)

# Thresholds
GET    /thresholds             → global caps + per-agent overrides + LiteLLM key budget/rpm
PUT    /thresholds             → write token/wall caps; budget+rpm via LiteLLM admin API

# Monitoring
GET    /tasks                  → recent runs (paginated) for the dashboard/runs table
GET    /metrics                → per-agent {activations, error_rate, usage_vs_cap, last_run}
                                 sourced from state.db + LiteLLM /spend + Langfuse API
```

**Validation rules (enforce server-side, surface in UI):**
- `model_alias` / `fallback_alias` must appear in `GET /models`.
- every `tools[]` name must exist in `TOOL_REGISTRY`.
- `id` is slug-shaped, unique, no `..` or `/`.
- trigger DAG across all active agents must be acyclic; at least one active `plan` and one active `synthesize` agent must remain after any edit/delete/deactivate (reject the mutation otherwise with a clear message).

**Persistence:** all writes go through the registry (§2.3) → JSON file. No process restart needed; the next `POST /tasks` reads fresh records.

---

## 5. Frontend

**Stack:** React + Vite + TypeScript + Tailwind. TanStack Query for server state. Shipped as its own container `hyperion-ui` (§6). No auth (localhost). React chosen for ecosystem familiarity — not load-bearing; Vue/Svelte would be fine.

**API base URL:** `http://localhost:4100` from the browser (host), injected as `VITE_HYPERION_API` at build/run.

### Pages

1. **Dashboard / Agent HQ** — the "centralized management" view the vision calls for.
   - Agent cards grouped by `group`: name, status pill (active/inactive), stage badge, model, trigger summary ("always" / "keyword: code,script" / "after researcher"), and live metrics (activations, error rate, usage bar vs. cap).
   - `Add Agent`, per-card `Edit` / `Duplicate` / `Activate-Deactivate` / `Delete`.
   - Filter by group / stage / active.

2. **Agent editor** (route or side drawer) — tabbed form, the "clear workflow design" the vision asks for:
   - *Identity:* name, id (auto-slug, locked after create), description, group, stage, order, active toggle.
   - *Prompt:* `goal` + `backstory` textareas (the system-prompt surface). Warn banner when editing a seeded core agent whose output other agents depend on (e.g. planner's YAML contract).
   - *Model & params:* model dropdown (`/models`), fallback override, temperature / top_p / max_tokens / max_iter.
   - *Tools:* multiselect from `/tools` with descriptions.
   - *Triggers:* type selector (`always` / `keyword` / `task_type` / `upstream` / `schedule`) with type-specific sub-forms; for `upstream`, a dropdown of other agents; live "cycle detected" validation echoing the server.
   - *Thresholds:* per-agent token/activation caps (blank = inherit global).

3. **Global Settings** — primary + fallback model (writes `/config`), global token/wall caps, LiteLLM virtual-key budget + rpm (writes `/thresholds`). Show provider-key status from `/config` (reuses existing payload).

4. **Monitoring** — recent runs table from `/tasks` (id, request, status, duration, selected agents); per-agent metric tiles from `/metrics`; **deep links to Langfuse** (`http://localhost:3001`) per task/session and per agent. Run-detail panel shows the `routing` explanation (§3.5): which agents fired and why others were skipped.

### UX principles (from the vision, kept)
- Clear status indicators (active/inactive, running, usage-vs-cap).
- Optimistic toggles for activate/deactivate; everything else confirm-on-save.
- Consistent design language; keyboard-accessible forms.

---

## 6. Docker & integration

Add a `hyperion-ui` service to `ai-router/docker-compose.yml`, modeled on the existing `hyperion` block (lines 210–243):

```yaml
  hyperion-ui:
    build:
      context: ../agents/hyperion-ui
      dockerfile: Dockerfile
    container_name: hyperion-ui
    restart: unless-stopped
    ports:
      - "4102:80"          # nginx serving the built SPA
    environment:
      VITE_HYPERION_API: "http://localhost:4100"
    depends_on:
      - hyperion
    networks:
      - ai-net
```

- Multi-stage Dockerfile: `node:20` build → `nginx:alpine` serve `dist/`.
- Mount the agents config dir into the **backend** containers (`hyperion`, `hyperion-mcp`), not the UI:
  ```yaml
  - ../agents/hyperion/config:/app/config:rw
  ```
- Add `HYPERION_CONFIG_DIR=/app/config` to both backend `environment:` blocks.
- The UI is a pure SPA; all state lives in the Hyperion API. No DB of its own.

**Gotcha reminder (from `CLAUDE.md`):** inside the Docker network use the container's *internal* port. The UI talks to the backend from the **browser** (host side), so `localhost:4100` is correct there; do not use `http://hyperion:4100` in `VITE_HYPERION_API`.

---

## 7. Repository layout

```
~/ai/agents/hyperion-ui/
├── PLAN.md                       ← this file
├── Dockerfile                    ← node build → nginx serve
├── nginx.conf
├── package.json
├── vite.config.ts
├── index.html
└── src/
    ├── main.tsx
    ├── api/                      ← typed client for the Hyperion API
    ├── pages/
    │   ├── Dashboard.tsx
    │   ├── AgentEditor.tsx
    │   ├── Settings.tsx
    │   └── Monitoring.tsx
    └── components/

~/ai/agents/hyperion/             ← backend changes live here
├── config/agents/*.json          ← NEW: seeded agent records (git-tracked)
└── src/hyperion/
    ├── agents/registry.py        ← NEW: AgentRecord model, load/save, TOOL_REGISTRY
    ├── crews/router.py           ← NEW: trigger evaluation + DAG builder
    ├── crews/default.py          ← MODIFIED: data-driven build_crew
    ├── scheduler.py              ← NEW: apscheduler loop for schedule triggers
    └── server/api.py             ← MODIFIED: agent/tools/models/thresholds/metrics endpoints
```

---

## 8. Phased implementation

Each phase ends with the acceptance check. Do not advance until it passes.

### Phase A — Data-driven agents (backend, no UI). *Gates everything.*
- `agents/registry.py`: `AgentRecord`, load/save/delete/validate, `TOOL_REGISTRY`.
- Seed `config/agents/*.json` from the current factory literals (planner/researcher/synthesizer active; developer/critic inactive).
- Rewrite `build_crew` to assemble from records (still sequential plan→work→synth for the seed).
- Mount config dir; add `HYPERION_CONFIG_DIR`.
- **Acceptance:** a run yields output identical to today; editing a JSON `backstory` and rerunning reflects the change; CrewAI still pinned, no new `Agent`/`Task` kwargs.

### Phase B — Routing engine (dynamic triggers).
- `crews/router.py`: stages, trigger evaluation, DAG topo-sort, cycle rejection.
- Planner emits `task_type` + `keywords` in `plan.md` front-matter.
- `GET /tasks/{id}` returns the `routing` explanation.
- **Acceptance:** an agent with `trigger: keyword:[code]` fires only when the request contains "code"; an `upstream` agent runs after its dependency; a cycle is rejected with a clear error; planner-only task (no work matches) still produces a result.

### Phase C — CRUD + options API.
- `/agents` CRUD, `/tools`, `/models`, `PUT /config`. Full server-side validation (§4).
- **Acceptance:** create/edit/delete/duplicate an agent via `curl`; invalid model/tool/cycle is rejected; new agent participates in the next run per its trigger.

### Phase D — UI: Dashboard + Editor + Settings.
- React app, three pages, typed API client, Docker service on `:4102`.
- **Acceptance:** full agent lifecycle in the browser (add → edit prompt/model/tools/trigger → activate → run → see expected routing/result); global model + caps editable.

### Phase E — Thresholds + Monitoring.
- `/thresholds` (token/wall caps + LiteLLM key budget/rpm via admin API), `/metrics`, `/tasks` list.
- Monitoring page: runs table, per-agent tiles, Langfuse deep-links, routing explanation panel.
- Schedule triggers via `scheduler.py`.
- **Acceptance:** set a per-agent token cap and see a run abort with `CapExceeded`; usage bars reflect real activity; Langfuse links resolve to the right session; a scheduled agent enqueues on cron.

### Phase F — Polish.
- Export/import (config-dir zip), group filters, notifications (threshold hit / agent failed) via UI toasts + optional webhook, agent-grouping UI.

---

## 9. Risks & open questions

1. **CrewAI 0.86 is pinned and breaks between minors** (`CLAUDE.md`). Data-driven `Agent`/`Task` construction must reuse the exact kwargs the current factories use. Do not upgrade CrewAI as part of this work.
2. **Prompt edits can break the run contract.** The planner's YAML front-matter (`task_type`, `subtasks`, `needs_review`) is now load-bearing for routing. The editor must warn before editing core-agent prompts, and the router must fail gracefully (treat missing `task_type` as `mixed`).
3. **At-least-one-plan / at-least-one-synthesize invariant.** Deleting/deactivating the last plan or synth agent must be rejected, or runs break. Enforce in API validation.
4. **Schedule triggers add a background loop** to the API process — ensure it doesn't interfere with the existing `asyncio.create_task` job runner; use a single AsyncIOScheduler started in the FastAPI `startup` hook.
5. **Metrics source of truth.** Activation counts come from `state.db`; cost/tokens from LiteLLM `/spend`; quality/trace from Langfuse. Decide caching (poll-on-load is fine for single-user v1).
6. **Config dir as the version-control story.** Relies on the user committing `config/agents/`. Acceptable for v1; a "snapshot/restore" button is Phase F if it proves needed.

---

## 10. Suggested order for Sonnet

1. Phase A in full — verify byte-identical behavior before touching anything dynamic.
2. Phase B — verify routing with `curl` and crafted triggers before building UI.
3. Phase C — verify CRUD + validation with `curl`.
4. Phase D — build the UI against the now-stable API.
5. Phase E — thresholds, metrics, scheduler.
6. Pause, demo to Charlie. Then Phase F.

Each step ends with its acceptance check. Do not advance until the previous acceptance passes.
