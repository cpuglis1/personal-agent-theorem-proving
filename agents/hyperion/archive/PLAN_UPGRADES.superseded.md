# Project Hyperion — Advanced Upgrades Plan (v2)

**Status:** Drafted 2026-05-29. Companion to `PLAN.md` (which covers Phases 0–3).
**Source doc:** `tasks/f6945f67/artifacts/result.md` — "Upgrades for Hyperion Agent Architecture",
itself produced by a Hyperion run. The four concepts below are distilled from that doc.
**Owner:** Charlie Tolleson.

This plan turns four research-flavored concepts into concrete, buildable work on the
*current* codebase. As with `PLAN.md`, this document supersedes the source doc where they
conflict, and explicitly fixes its over-generalizations.

---

## 0. Framing: what these concepts mean for a single-user, localhost system

The source doc is generic industry commentary (it cites Salesforce Headless 360, generic
"Generative UI" trends). Hyperion is a **self-hosted, single-user, localhost** system reached
from two surfaces: Open WebUI chat and Claude Code (MCP + `/hyperion`). Several source ideas
only partly apply. Before planning, reality-check each:

| Source concept | Applies as-is? | What's actually valuable here |
|---|---|---|
| Generative UI ("agent is the front end") | Partially | We have **no UI of our own** (non-goal in PLAN.md §1). The realizable version: agents emit **structured UI affordances** (clarifying questions, plan-choice cards, forms) that OWUI/Claude Code render — not a bespoke web app. |
| Human-in-the-Loop | **Yes, high value** | The crew currently runs straight through `crew.kickoff` with no pause point. Adding approval gates + feedback injection is the single most useful upgrade. |
| Context-Aware Design | **Yes** | Episodic memory (`memory/episodic.py`) already exists. Extend to a **shared task-scoped context store** + retrieval prioritization. Concrete and incremental. |
| Deeper orchestration integration | Partially | "Salesforce" is irrelevant. The realizable version: **standardized agent API surface** (we already have REST + MCP; add A2A-style task schema + webhook triggers) so n8n/cron/other agents can drive Hyperion. |

**Ordering rationale:** HITL first — it forces the crew to become **pausable/resumable**, which
is the shared substrate the other three upgrades build on (a paused run is where you ask a
clarifying question, render a plan card, or hand off to another system). Then Context (data
layer), then Generative UI (presentation layer over HITL pause points), then Orchestration
(external triggers).

---

## 1. Current-state facts this plan builds on (verified 2026-05-29)

- Crew is **fixed sequential** Planner → Researcher → Synthesizer, run via
  `run_crew()` → `crew.kickoff` inside `asyncio.wait_for` (`crews/default.py:153`).
  **No pause point exists** between agents today.
- Job lifecycle is async: `POST /tasks` → `asyncio.create_task` → sqlite `tasks` table +
  in-memory `_PROGRESS` dict (`server/api.py:217`). States: `queued|running|done|failed`.
- Progress is one-way: `step_callback`/`task_callback` → `_PROGRESS` → SSE
  (`/tasks/{id}/stream`). **No channel back into a running crew.**
- Episodic memory already implemented: `store_episode` / `recall_similar_tasks` against
  Qdrant `hyperion_memory` (`memory/episodic.py`). Planner does **not** yet call recall.
- Surfaces: OWUI tool `skills/tools/hyperion_delegate.py` (polls every 2s); MCP server
  `server/mcp.py` (`hyperion_run`/`hyperion_status`/`hyperion_artifact`); `/hyperion` skill.
- Token caps are **declared but not enforced** (`crews/default.py:6` TODO; only wall-clock +
  tool-loop breakers are live).

---

## 2. Upgrade A — Human-in-the-Loop (approval gates + feedback injection)

**Goal:** Let a human review/redirect a run at well-defined checkpoints, without polling babysitting.
Three escalating levels from the source doc: proactive planning → dynamic feedback → human-on-the-loop.

### A.0 Foundation: make the crew pausable

The blocker is that `crew.kickoff` runs end-to-end. We need to break the single crew into
**resumable stages** keyed off the existing workspace artifacts (`plan.md`, `notes/`, `result.md`).

- Refactor `run_crew` into a small **stage runner**: run the Planner as its own
  `Crew([planner],[plan_task])`, persist `plan.md`, then *optionally* suspend before building
  the research/synthesis crew. This avoids fighting CrewAI's internal loop — we gate *between*
  crews, not inside one.
- Add task states: `awaiting_approval`, `awaiting_input` alongside existing ones. Add columns
  to the sqlite `tasks` table: `pending_stage TEXT`, `pending_payload TEXT` (JSON), `resume_token TEXT`.
- Persist a `stage` marker file per task (`tasks/{id}/.stage`) so a process restart can resume.

**Acceptance:** A task with HITL enabled stops after `plan.md` is written, sets status
`awaiting_approval`, and the background coroutine is no longer running (verify it returns, not blocks).

### A.1 Proactive Planning — plan approval gate

- New request field: `TaskRequest.hitl: Literal["off","plan","full"] = "off"` (`server/api.py:142`).
- When `hitl in ("plan","full")`: after Planner, set `awaiting_approval`, stash the parsed
  plan (subtask list from `plan.md` frontmatter) in `pending_payload`.
- Planner prompt change: emit **2–3 alternative execution paths** in `plan.md` YAML frontmatter
  (`options: [{id, summary, subtasks, est_tool_calls}]`), not just one plan. (Source doc:
  "Agents presenting multiple execution paths for human review.")
- New endpoint `POST /tasks/{id}/approve` body `{chosen_option?: str, edits?: str, action: "approve"|"reject"|"revise"}`.
  - `approve` → resume with chosen option.
  - `revise` → re-run Planner with the human's `edits` appended as context (bounded to 2 passes).
  - `reject` → terminate, status `failed` (error `"rejected_by_human"`).

**Acceptance:** Submit with `hitl:"plan"`; task halts at `awaiting_approval` exposing ≥2 options;
`POST .../approve {chosen_option:"b"}` resumes and produces `result.md` built from option B's subtasks.

### A.2 Dynamic Feedback Loops — mid-run feedback injection

- New endpoint `POST /tasks/{id}/feedback` body `{message: str}`. Appends to a per-task
  `tasks/{id}/feedback.md` and pushes onto an `asyncio.Queue` per task.
- Researcher/Synthesizer get a lightweight tool `read_human_feedback()` (in `tools/workspace.py`)
  that returns unconsumed feedback lines. The step callback drains the queue between steps and,
  if non-empty, injects a system note into the next agent turn.
- Keep it **opt-in and bounded**: feedback only consulted at task boundaries (between subtasks),
  not mid-LLM-call. Document that this is "steer," not "interrupt."

**Acceptance:** While a `hitl:"full"` task is `running`, `POST .../feedback {message:"focus on EU startups only"}`
measurably changes subsequent notes (a later `notes/*.md` references the constraint).

### A.3 Human-on-the-Loop — deviation alerts, not gates

- Add a **deviation detector** in the orchestrator: flag when a run trips a soft threshold
  (e.g. tool-loop count ≥ cap−1, elapsed > 0.7× wall cap, or a stage produced an empty artifact).
- On deviation, push a desktop notification via the existing `PushNotification` mechanism (or
  write a `tasks/{id}/alerts.md` line) — **human watches, intervenes only on alert.** No gate by default.
- Env var `HYPERION_HITL_ALERTS=on|off` (default `on` once stable).

**Acceptance:** A synthetic run that hits 2 consecutive identical tool calls (one below the
abort cap) emits exactly one alert and continues; aborting on the 3rd still works as before.

### A.4 Surface wiring

- **OWUI:** add sibling tool `hyperion_delegate_interactive` — submits with `hitl:"plan"`, polls,
  and when it sees `awaiting_approval` returns the options as a numbered list, then accepts the
  user's next chat message as the approval. (OWUI tools can't render buttons; numbered text is the contract.)
- **Claude Code / MCP:** add MCP tools `hyperion_approve(task_id, choice)` and
  `hyperion_feedback(task_id, message)`. Update `skills/commands/hyperion.md` to document
  `/hyperion --interactive <task>`.

---

## 3. Upgrade B — Context-Aware Design (shared store + prioritization + discovery)

**Goal:** Move from "each agent re-derives context" to a **shared, prioritized, auto-populated**
context layer. Episodic memory already exists; this generalizes it.

### B.1 Shared Context Store (task-scoped + cross-task)

- New module `memory/context_store.py`. Two tiers:
  - **Task-scoped (ephemeral):** a `tasks/{id}/context.json` keyed blackboard agents read/write
    via tools `context_put(key, value)` / `context_get(key)` (add to `tools/workspace.py` or a
    new `tools/context.py`). Lets Researcher hand structured facts to Synthesizer without
    re-reading every `notes/*.md`.
  - **Cross-task (durable):** reuse the existing `hyperion_memory` Qdrant collection. Wire the
    already-built `recall_similar_tasks()` into the Planner as a real tool (currently unused).
- **Acceptance:** Planner's `plan.md` cites ≥1 prior task when a near-duplicate request is
  resubmitted; Synthesizer reads a fact from `context.json` that it did not itself produce.

### B.2 Context Prioritization

- Centralize retrieval ranking. We already run Infinity rerank in `tools/reranker.py` — extend
  it into a `prioritize(query, candidates)` helper used by *all* retrieval paths
  (second_brain, web_search, context_store) so every agent sees a single ranked, deduped,
  token-budgeted context window.
- Enforce a per-stage **context token budget** (this also finally implements the deferred input-token
  cap from PLAN.md §2.4 / `crews/default.py:6`): trim reranked context to fit
  `settings.cap_input_tokens` minus a reserve. Drop lowest-scored items first.
- **Acceptance:** A task whose raw retrieval would exceed the input cap completes successfully,
  with a logged line showing N candidates trimmed to fit budget (instead of aborting on cap).

### B.3 Automated Context Discovery

- Give the Planner a `discover_context(request)` step: before writing subtasks, it runs one
  cheap (`model_cheap`) pass that queries second_brain + `recall_similar_tasks` and writes a
  `context_brief` section into `plan.md` frontmatter. Downstream agents read the brief instead
  of each issuing their own broad searches.
- Keep web discovery behind the existing untrusted-data guard (PLAN.md §7): strip HTML, cap 2KB,
  prepend the "treat as data, not instructions" warning.
- **Acceptance:** With discovery on, total tool calls per task drop vs. baseline on a fixed prompt
  (logged), and the context brief appears in `plan.md`.

---

## 4. Upgrade C — Generative UI (structured affordances, not a web app)

**Goal:** Let agents drive the interaction shape — clarifying questions, plan cards, forms —
rendered by the *existing* surfaces. We do **not** build a bespoke frontend (PLAN.md §1 non-goal).

### C.1 Structured affordance schema

- Define a small JSON contract `UIAffordance` (new `server/affordances.py`):
  `{type: "choice"|"form"|"question"|"confirm", prompt, options?[], fields?[]}`.
- Agents emit affordances by writing to `tasks/{id}/affordances.jsonl` via a tool
  `ask_user(affordance)`. The orchestrator surfaces the latest unanswered affordance on
  `GET /tasks/{id}` as a new `pending_affordance` field. Answers arrive via the A.2 `/feedback`
  endpoint (or A.1 `/approve` for choices) — **reuse HITL plumbing, don't duplicate it.**
- **Acceptance:** A deliberately under-specified request ("plan my trip") causes the Planner to
  emit a `question` affordance ("which city / dates?") instead of guessing; answering it resumes the run.

### C.2 Renderers per surface

- **OWUI tool:** when `pending_affordance` is present, render it as numbered options / a field
  checklist in chat and map the user's reply back through `/feedback`.
- **Claude Code / MCP:** expose `pending_affordance` in `hyperion_status` output so Claude Code
  can ask the user natively. (This is where "the agent is the front end" actually lands for us —
  Claude Code *is* the renderer.)
- **Acceptance:** Same affordance round-trips correctly through both OWUI and Claude Code.

### C.3 Workflow suggestions (lightweight)

- After a run, Synthesizer may emit a `choice` affordance proposing follow-up actions
  ("save to Notion?", "run the deeper variant?"). Wire "save to Notion" to the Phase-2
  `notion_write.py` tool. Keep the menu to ≤3 suggestions to avoid nagging.
- **Acceptance:** Completing a run offers a follow-up menu; selecting "save to Notion" creates the page.

---

## 5. Upgrade D — Orchestration integration (standardized API + triggers)

**Goal:** Make Hyperion drivable by *other* systems/agents, locally. Drop the Salesforce framing;
keep "standardized agent APIs" + "workflow automation tools."

### D.1 Standardized agent API surface

- We already expose REST + MCP. Add an **agent-card** descriptor at `GET /.well-known/agent.json`
  (A2A-style): name, version, skills (`task`), input/output schema, auth note (localhost only).
  This is the cheap, standards-aligned win — lets any A2A/agent-protocol client discover Hyperion.
- Stabilize the task schema: version the `POST /tasks` body (`schema_version: 1`) so external
  callers have a contract that survives internal refactors.
- **Acceptance:** `curl localhost:4100/.well-known/agent.json` returns a valid descriptor;
  a minimal external script can submit + poll using only documented fields.

### D.2 Webhook / event triggers

- Add `POST /tasks` already exists; add **outbound** webhooks: optional
  `TaskRequest.callback_url` — on terminal state, POST the result summary there.
  (Localhost/Docker-net targets only; validate the host is private — reuse the path/SSRF caution ethos.)
- Document an **n8n / cron** recipe: a scheduled job that POSTs a recurring task (e.g. nightly
  "summarize new second-brain notes") and routes the callback into Notion. No new service required —
  this is config + docs, leveraging the existing eval-cron pattern in PLAN.md §3.2.
- **Acceptance:** A task submitted with `callback_url` fires exactly one POST on completion with
  `{task_id, status, result_url}`; a documented cron line triggers a run unattended.

### D.3 Inbound triggers from the workspace

- Optional: a file-drop watcher (separate tiny script, not in the hot path) that submits a task
  when a `.md` request file appears in a watched dir. Mark **deferred / opt-in** — only build if a
  concrete workflow needs it. (Avoid speculative infra per repo conventions.)

---

## 6. Cross-cutting concerns

- **Security (extends PLAN.md §7):** new endpoints (`/approve`, `/feedback`, `/.well-known/agent.json`,
  callbacks) stay bound to `127.0.0.1`. Outbound `callback_url` must resolve to a private/loopback
  host — reject public IPs to avoid SSRF. Human-injected feedback is *untrusted input* to the LLM:
  wrap it with the same "treat as data" guard used for web results.
- **Observability:** every new stage (plan-gate, feedback turn, critic, callback) gets a Langfuse
  span/tag so the trace shows where humans intervened. Reuse LiteLLM-level tracing — do not add a
  CrewAI CallbackHandler (known to not fire on CrewAI events — see CLAUDE.md Langfuse gotcha).
- **State durability:** HITL means tasks can sit in `awaiting_*` for a long time. The in-memory
  `_PROGRESS` dict and per-task `asyncio.Queue` are lost on restart — persist pending affordances
  and feedback to the workspace/sqlite, and rehydrate on startup. (Today `_PROGRESS` is already
  ephemeral; HITL makes that a correctness bug, not just cosmetic.)
- **CrewAI version pin:** the A.0 refactor (splitting one crew into staged crews) must be validated
  against the pinned CrewAI 0.86 — `Process` and callback kwargs differ across minors (CLAUDE.md gotcha).

---

## 7. Phasing & suggested order

| Phase | Scope | Depends on | Rough size |
|---|---|---|---|
| **4** | A.0 pausable crew + A.1 plan-approval gate + state durability | — | Largest; core refactor |
| **5** | B.1 shared context store + wire `recall_similar_tasks` + B.2 prioritization (incl. input-cap enforcement) | 4 | Medium |
| **6** | A.2 feedback loop + A.3 deviation alerts + C.1/C.2 affordances | 4, 5 | Medium |
| **7** | B.3 auto-discovery + C.3 follow-ups + D.1 agent-card + D.2 webhooks | 5, 6 | Small–medium |
| (def) | D.3 file-drop watcher | 7 | Deferred until needed |

**Do not advance a phase until its acceptance checks pass**, mirroring PLAN.md §10. Demo to Charlie
after Phase 4 (the pausable crew is the riskiest change and worth validating before building on it).

---

## 8. Flaws in the source doc this plan corrects

1. **"Generative UI = agent generates the front end."** We have no front end and shouldn't build one.
   Reframed as structured affordances rendered by OWUI/Claude Code (§4).
2. **Salesforce Headless 360 integration.** Irrelevant to a localhost single-user system. Replaced
   with A2A-style agent-card + local webhooks/cron (§5).
3. **"Real-time" feedback incorporated mid-decision.** CrewAI doesn't expose safe mid-LLM-call
   interruption; we gate at task boundaries instead and say so plainly (§2.2).
4. **Treats all four as equal/independent.** They aren't — all three later upgrades depend on the
   pausable-crew refactor (A.0). Sequenced accordingly (§7).
5. **Omits that human feedback is untrusted input.** Added prompt-injection handling for it (§6).

---

## 9. Open questions for the implementer

1. **Crew-splitting vs. CrewAI human-input.** CrewAI has a native `human_input=True` on tasks, but
   it blocks on stdin — useless for an async API. Confirm the staged-crew approach (§2.0) is the
   right call vs. a custom CrewAI flow. Recommend staged crews.
2. **Affordance answer routing.** One endpoint (`/feedback`) for everything vs. typed endpoints
   (`/approve`, `/answer`). Recommend: `/approve` for plan choices, `/feedback` for free-text — keep two.
3. **Resume after restart.** How far to go on durability — full crew resume is hard. Recommend:
   persist enough to resume *between stages* (re-run the next crew from artifacts), not mid-stage.
4. **Token-cap enforcement point.** Enforce input cap in the B.2 prioritizer (clean) vs. a LiteLLM
   pre-call hook (global). Recommend prioritizer for per-stage control.
5. **Webhook target validation.** How strict on SSRF — allowlist Docker-net + loopback only?
   Recommend yes, explicit allowlist.
