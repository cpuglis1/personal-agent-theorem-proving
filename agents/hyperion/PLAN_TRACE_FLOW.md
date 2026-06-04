# Hyperion Trace Flow View — Implementation Plan

## Goal

Add a **Trace Flow** page to the Hyperion UI (`localhost:4102`) that renders all LLM
calls for a task as an interactive React Flow DAG. Each node shows the agent name,
model, token counts, and cost estimate. Hovering a node reveals the full prompt,
response, and tools used. User prompts and internal meta-prompts are styled
distinctly.

This replaces Langfuse as the primary per-task trace viewer. Langfuse remains as a
secondary tool — we are not modifying it.

---

## Repo Layout (what exists today)

```
~/ai/
├── agents/
│   ├── hyperion/               ← FastAPI backend (Python, port 4100)
│   │   └── src/hyperion/
│   │       ├── server/api.py   ← FastAPI app + SQLite (tasks table)
│   │       ├── usage.py        ← HyperionUsageLogger (CustomLogger)
│   │       ├── llms.py         ← _make_llm(), extra_body.metadata tagging
│   │       ├── crews/runner.py ← run_task(), workflow DAG execution
│   │       └── config.py       ← settings (tasks_dir, model aliases, etc.)
│   └── hyperion-ui/            ← React/Tailwind/Vite frontend (port 4102)
│       └── src/
│           ├── App.tsx          ← routes
│           ├── api/client.ts    ← typed fetch hooks (react-query)
│           └── pages/
│               ├── RunDetail.tsx   ← existing per-task detail page
│               └── ...
└── ai-router/docker-compose.yml ← hyperion on :4100, hyperion-ui on :4102
```

### Key backend facts

- `server/api.py` owns the SQLite DB at `settings.tasks_dir / "state.db"`.
- Table `tasks` columns: `task_id, status, request, error, result_path, created_at,
  updated_at, hitl, pending_stage, pending_payload, resume_token, routing, callback_url,
  workflow`. The `routing` column stores a JSON blob with `selected_agents`, `skipped`,
  and `dag` (adjacency list of agent IDs → upstream agent IDs).
- `usage.py`'s `HyperionUsageLogger` is a LiteLLM `CustomLogger`. Its
  `log_success_event` already extracts `task_id`, `agent_role`, `in_tokens`,
  `out_tokens` from `extra_body.metadata`.
- `llms.py`'s `_make_llm()` sets `extra_body.metadata` with `session_id` (= task_id),
  `tags` (= `["hyperion", agent_role]`), and `generation_name`.
- `runner.py`'s `run_task()` returns a dict; after the final synthesize stage it calls
  `_write_fallback_result()` and returns `{"status": "done", "result_path": ..., ...}`.
  The entry point is line ~629.

### Key frontend facts

- React 18 + Vite + Tailwind + `@tanstack/react-query` v5.
- `api/client.ts` has a typed `req<T>()` helper and all existing hooks (`useTask`,
  `useTasks`, etc.).
- `App.tsx` uses `react-router-dom` v6. Existing route: `runs/:id` → `RunDetail`.
- Design: dark theme, `card` CSS class (`bg-surface border border-edge rounded-xl p-4`
  pattern from index.css and other pages).

---

## Work Breakdown

### Part 1 — SQLite: `trace_events` table

**File:** `src/hyperion/server/api.py`

Add this table creation inside the existing `_migrate()` coroutine (alongside the
`ALTER TABLE` logic that adds nullable columns):

```sql
CREATE TABLE IF NOT EXISTS trace_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT    NOT NULL,
    agent_role       TEXT    NOT NULL,
    prompt_type      TEXT    NOT NULL DEFAULT 'user-facing',
    model            TEXT,
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    cost_usd         REAL    DEFAULT 0.0,
    prompt_preview   TEXT,
    response_preview TEXT,
    tools_used       TEXT,
    started_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    duration_ms      INTEGER
)
```

`prompt_type` is either `"user-facing"` (main crew agents) or `"meta-prompt"` (internal
post-processing calls). It is derived from the `tags` list in `extra_body.metadata`:
if `"meta-prompt"` is in the tags → `"meta-prompt"`, else → `"user-facing"`.

---

### Part 2 — Write trace rows in `usage.py`

**File:** `src/hyperion/usage.py`

Extend `HyperionUsageLogger.log_success_event` to write a `trace_events` row after
the existing `record()` call.

The method already has `kwargs` (LiteLLM call kwargs) and `response_obj`. Extract:

| Column | Source |
|--------|--------|
| `task_id` | `_attribution(kwargs)[0]` (already called) |
| `agent_role` | `_attribution(kwargs)[1]` (already called) |
| `prompt_type` | `"meta-prompt"` if `"meta-prompt"` in `_dig_metadata(kwargs).get("tags", [])` else `"user-facing"` |
| `model` | `kwargs.get("model", "")` |
| `input_tokens` | already extracted as `in_tok` |
| `output_tokens` | already extracted as `out_tok` |
| `cost_usd` | `litellm.completion_cost(completion_response=response_obj)` wrapped in try/except (returns 0.0 on failure) |
| `prompt_preview` | last message content from `kwargs.get("messages", [])`, truncated to 500 chars |
| `response_preview` | `response_obj.choices[0].message.content[:500]` wrapped in try/except |
| `tools_used` | `json.dumps([t["function"]["name"] for t in response_obj.choices[0].message.tool_calls or []])` wrapped in try/except, else `"[]"` |
| `started_at` | `start_time.isoformat()` |
| `duration_ms` | `int((end_time - start_time).total_seconds() * 1000)` |

**DB write:** Import `aiosqlite` and write synchronously using `sqlite3` (since
`log_success_event` is sync). Use the same DB path: `settings.tasks_dir / "state.db"`.

```python
import sqlite3, json, litellm as _ll
from hyperion.config import settings

# inside log_success_event, after record(task_id, role, in_tok, out_tok):
try:
    meta = _dig_metadata(kwargs)
    tags = meta.get("tags") or []
    prompt_type = "meta-prompt" if "meta-prompt" in tags else "user-facing"
    
    messages = kwargs.get("messages") or []
    prompt_preview = ""
    if messages:
        last = messages[-1]
        content = last.get("content") or ""
        if isinstance(content, list):          # multi-part content
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        prompt_preview = str(content)[:500]
    
    response_preview = ""
    tools_used = "[]"
    try:
        choice = response_obj.choices[0]
        response_preview = (choice.message.content or "")[:500]
        tc = getattr(choice.message, "tool_calls", None) or []
        tools_used = json.dumps([t.function.name for t in tc])
    except Exception:
        pass
    
    try:
        cost = _ll.completion_cost(completion_response=response_obj)
    except Exception:
        cost = 0.0
    
    duration_ms = int((end_time - start_time).total_seconds() * 1000)
    
    db_path = str(settings.tasks_dir / "state.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO trace_events
               (task_id, agent_role, prompt_type, model, input_tokens, output_tokens,
                cost_usd, prompt_preview, response_preview, tools_used,
                started_at, duration_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, role, prompt_type, kwargs.get("model",""),
             in_tok, out_tok, cost or 0.0,
             prompt_preview, response_preview, tools_used,
             start_time.isoformat(), duration_ms),
        )
        conn.commit()
except Exception:
    pass   # tracing must never crash the main path
```

Also extend `async_log_success_event` to call the sync method (already the pattern in
the existing code).

---

### Part 3 — Meta-task pipeline

**Context:** OWUI's built-in title generation, follow-up suggestions, and tag
generation are currently enabled in OWUI Admin Panel → Settings. They call the LiteLLM
proxy directly as untagged calls, which is why they appear in Langfuse without
differentiation.

**Step 3a — Disable in OWUI:**  
In OWUI Admin Panel → Settings, disable: "Title Auto-Generation", "Tags Generation",
and "Follow-up Suggestions" (exact label names may vary by OWUI version).

**Step 3b — New file:** `src/hyperion/server/meta_tasks.py`

```python
"""
meta_tasks.py — configurable post-run meta-prompt pipeline.

Each entry in META_TASKS fires after the synthesizer stage completes.
All tasks run in parallel (asyncio.gather) against the synthesizer's result text only
(not the full conversation — kept cheap).

To add a meta-task: append an entry to META_TASKS.
To disable one:    set "enabled": False.
To edit a prompt:  edit the "prompt" string.

Results are saved to tasks/{task_id}/meta/{task_id}.txt.
The LLM calls are tagged with ["hyperion", "meta-prompt", <id>] so they appear
as prompt_type="meta-prompt" in trace_events and the Trace Flow UI.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

META_TASKS: list[dict[str, Any]] = [
    {
        "id": "title",
        "enabled": True,
        "prompt": (
            "Generate a concise 3-5 word title with a relevant emoji for the following "
            "research output. Output only the title, nothing else.\n\n{result}"
        ),
    },
    {
        "id": "followups",
        "enabled": True,
        "prompt": (
            "Based on the following research output, suggest 3-5 follow-up questions a "
            "user might want to explore next. Output a numbered list only.\n\n{result}"
        ),
    },
    {
        "id": "tags",
        "enabled": True,
        "prompt": (
            "Analyze the following research output and provide 1-3 broad topic tags and "
            "1-3 specific subtopic tags. Format: broad: tag1, tag2 | specific: tag3, tag4"
            "\n\n{result}"
        ),
    },
]


async def _run_one(task_id: str, meta: dict, result_text: str, out_dir: Path) -> None:
    from hyperion.llms import _make_llm
    from hyperion.config import settings

    llm = _make_llm(
        settings.model_cheap,
        temperature=0.0,
        task_id=task_id,
        agent_role=f"meta/{meta['id']}",
        # The extra_body tags are built inside _make_llm from agent_role — but we
        # need "meta-prompt" in the tags list for trace_events classification.
        # We override by passing extra kwargs that _make_llm forwards to HyperionLLM.
    )
    # Inject "meta-prompt" tag by patching extra_body directly after construction.
    if hasattr(llm, "extra_body") and isinstance(llm.extra_body, dict):
        meta_block = llm.extra_body.get("metadata", {})
        tags = list(meta_block.get("tags", []))
        if "meta-prompt" not in tags:
            tags.append("meta-prompt")
        meta_block["tags"] = tags

    prompt = meta["prompt"].format(result=result_text[:4000])
    try:
        response = llm.call([{"role": "user", "content": prompt}])
        text = response if isinstance(response, str) else getattr(response, "content", str(response))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{meta['id']}.txt").write_text(text.strip(), encoding="utf-8")
        logger.info("task %s: meta/%s complete", task_id, meta["id"])
    except Exception as exc:
        logger.warning("task %s: meta/%s failed: %s", task_id, meta["id"], exc)


async def run_meta_tasks(task_id: str, result_text: str) -> None:
    """Run all enabled meta-tasks in parallel. Called after synthesize completes."""
    from hyperion.config import settings

    enabled = [m for m in META_TASKS if m.get("enabled", True)]
    if not enabled or not result_text.strip():
        return

    out_dir = settings.tasks_dir / task_id / "meta"
    await asyncio.gather(*[_run_one(task_id, m, result_text, out_dir) for m in enabled])
```

**Note on the "meta-prompt" tag injection:** `_make_llm` builds `extra_body` from
`agent_role`. The cleanest fix is to add an optional `extra_tags: list[str]` parameter
to `_make_llm` that gets merged into the tags list. This avoids the post-construction
patch shown above. Implement whichever approach is cleaner.

**Step 3c — Call from runner:** In `runner.py`, after `_write_fallback_result` succeeds
(i.e., just before the final `return {"status": "done", ...}` at line ~630), read the
result file and fire meta-tasks:

```python
# After _write_fallback_result:
result_text = ""
if result_path:
    try:
        result_text = Path(result_path).read_text(encoding="utf-8")
    except Exception:
        pass
if result_text:
    try:
        from hyperion.server.meta_tasks import run_meta_tasks
        await run_meta_tasks(task_id, result_text)
    except Exception as exc:
        logger.warning("task %s: meta_tasks failed: %s", task_id, exc)
```

---

### Part 4 — API endpoint

**File:** `src/hyperion/server/api.py`

Add `GET /tasks/{task_id}/trace`:

```python
@app.get("/tasks/{task_id}/trace")
async def get_task_trace(task_id: str) -> dict:
    """Return trace events + DAG structure for the Trace Flow UI."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Task metadata
        async with db.execute(
            "SELECT request, status, routing FROM tasks WHERE task_id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Task not found")
        
        request_text = row["request"] or ""
        routing = json.loads(row["routing"]) if row["routing"] else None
        
        # Trace events
        async with db.execute(
            """SELECT agent_role, prompt_type, model, input_tokens, output_tokens,
                      cost_usd, prompt_preview, response_preview, tools_used,
                      started_at, duration_ms
               FROM trace_events
               WHERE task_id=?
               ORDER BY started_at""",
            (task_id,),
        ) as cur:
            events = [dict(r) for r in await cur.fetchall()]
        
        # Parse tools_used from JSON string back to list
        for ev in events:
            try:
                ev["tools_used"] = json.loads(ev["tools_used"] or "[]")
            except Exception:
                ev["tools_used"] = []

    return {
        "task_id": task_id,
        "request": request_text,
        "status": row["status"],
        "routing": routing,
        "events": events,
    }
```

---

### Part 5 — Frontend: install React Flow

**File:** `agents/hyperion-ui/package.json`

Add to `dependencies`:
```json
"@xyflow/react": "^12.0.0"
```

Run `npm install` (or `pnpm install`) in `agents/hyperion-ui/`.

React Flow v12 is published under `@xyflow/react`. Import: `import { ReactFlow, ... } from "@xyflow/react"`. Include the bundled CSS: `import "@xyflow/react/dist/style.css"`.

---

### Part 6 — Frontend: types + hook

**File:** `agents/hyperion-ui/src/api/client.ts`

Add to the bottom:

```typescript
// ---------------------------------------------------------------------------
// Trace Flow
// ---------------------------------------------------------------------------

export interface TraceEvent {
  agent_role: string;          // e.g. "planner", "researcher", "meta/title"
  prompt_type: "user-facing" | "meta-prompt";
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  prompt_preview: string | null;
  response_preview: string | null;
  tools_used: string[];
  started_at: string;
  duration_ms: number | null;
}

export interface RoutingResult {
  selected_agents: string[];
  skipped: { id: string; reason: string }[];
  dag: Record<string, string[]>;   // agent_id → [upstream agent_ids]
}

export interface TraceResponse {
  task_id: string;
  request: string;
  status: string;
  routing: RoutingResult | null;
  events: TraceEvent[];
}

export function useTraceEvents(taskId: string | undefined) {
  return useQuery({
    queryKey: ["trace", taskId],
    queryFn: () => req<TraceResponse>(`/tasks/${taskId}/trace`),
    enabled: !!taskId,
  });
}
```

Note: `RoutingResult` is already defined in `client.ts` as part of `TaskResponse`. If
so, remove the duplicate and reuse the existing type.

---

### Part 7 — Frontend: TraceFlow page

**New file:** `agents/hyperion-ui/src/pages/TraceFlow.tsx`

#### Data → graph mapping

The response has:
- `request`: the user's original prompt (renders as a special "User Prompt" root node)
- `routing.dag`: adjacency list `{ agent_id: [upstream_agent_id, ...] }` — defines
  edges between crew nodes
- `events`: one row per LLM call — map onto nodes by `agent_role`

**Node types:**

| Type | Criteria | Style |
|------|----------|-------|
| `userPrompt` | Always the root; no `agent_role` | Blue accent border, no tokens |
| `agentNode` | `prompt_type === "user-facing"` | Standard card, blue/purple icon |
| `metaNode` | `prompt_type === "meta-prompt"` | Muted grey, dashed border, ⚙ icon, smaller |

**Layout algorithm:**

1. Start with the user prompt node at the top (y=0).
2. Group crew nodes by stage: `plan` nodes at y=120, `work` nodes at y=240, `synthesize`
   at y=360. Within each row, distribute x evenly.
3. Meta nodes form a final row at y=480, distributed evenly.
4. Edges: user prompt → first plan node(s); within crew, derive from `routing.dag`
   (reverse the adjacency: dag maps node → upstreams, so edge goes upstream → node);
   synthesize node → each meta node.

If `routing` is null (task failed early or is still running), render only the events
that do exist, with simple linear layout.

**Agent display name:** `agent_role` comes from the `tags` in `extra_body.metadata`
(e.g. `"planner"`, `"researcher"`, `"meta/title"`). Map to human names using the
agent registry or a local lookup. Provide a fallback that title-cases the role:
`role.split("/").pop().replace(/_/g, " ")`.

**Cost formatting:** `< $0.001` → `"< $0.001"`, else `$X.XXX`.

**Token formatting:** `1204` → `1.2k` for counts ≥ 1000.

#### Component sketch

```tsx
import { ReactFlow, Background, Controls, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useParams, Link } from "react-router-dom";
import { useTraceEvents } from "../api/client";

// Custom node components -------------------------------------------------------

function AgentNodeCard({ data }: { data: AgentNodeData }) {
  // data: { label, model, inputTokens, outputTokens, costUsd,
  //         promptPreview, responsePreview, toolsUsed, durationMs }
  // Normal card + hover tooltip via Tailwind group/group-hover
  return (
    <div className="group relative w-52 rounded-xl border border-edge bg-surface p-3 shadow-lg">
      {/* Main content always visible */}
      <div className="flex items-center gap-2 mb-1">
        <span className="text-blue-400 text-base">🤖</span>
        <span className="font-semibold text-sm text-slate-100 truncate">{data.label}</span>
      </div>
      <div className="text-xs text-slate-400 truncate mb-2">{data.model}</div>
      <div className="flex gap-3 text-xs text-slate-300">
        <span>↑ {fmtTokens(data.inputTokens)}</span>
        <span>↓ {fmtTokens(data.outputTokens)}</span>
        <span className="text-emerald-400">{fmtCost(data.costUsd)}</span>
      </div>

      {/* Tooltip on hover */}
      <div className="absolute left-full top-0 ml-3 z-50 hidden group-hover:block
                      w-80 rounded-xl border border-edge bg-surface-elevated p-4 shadow-2xl text-xs">
        <div className="font-semibold text-slate-100 mb-1">{data.label}</div>
        <div className="text-slate-400 mb-3">{data.model} · {data.durationMs}ms</div>
        {data.promptPreview && (
          <>
            <div className="text-slate-500 mb-1">Input ({fmtTokens(data.inputTokens)} tokens):</div>
            <div className="text-slate-300 max-h-28 overflow-auto mb-3 whitespace-pre-wrap
                            font-mono text-[11px] bg-black/30 rounded p-2">
              {data.promptPreview}
            </div>
          </>
        )}
        {data.responsePreview && (
          <>
            <div className="text-slate-500 mb-1">Output ({fmtTokens(data.outputTokens)} tokens):</div>
            <div className="text-slate-300 max-h-28 overflow-auto mb-3 whitespace-pre-wrap
                            font-mono text-[11px] bg-black/30 rounded p-2">
              {data.responsePreview}
            </div>
          </>
        )}
        {data.toolsUsed.length > 0 && (
          <div className="flex gap-1 flex-wrap">
            {data.toolsUsed.map(t => (
              <span key={t} className="rounded bg-violet-900/50 px-1.5 py-0.5 text-violet-300">{t}</span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function MetaNodeCard({ data }: { data: AgentNodeData }) {
  // Same structure but smaller, grey/dashed styling, ⚙ icon
  return (
    <div className="group relative w-44 rounded-xl border border-dashed border-slate-600
                    bg-slate-900/60 p-2.5 shadow">
      <div className="flex items-center gap-1.5 mb-1">
        <span className="text-slate-500 text-sm">⚙</span>
        <span className="font-medium text-xs text-slate-400 truncate">{data.label}</span>
      </div>
      <div className="flex gap-2 text-[11px] text-slate-500">
        <span>↑ {fmtTokens(data.inputTokens)}</span>
        <span>↓ {fmtTokens(data.outputTokens)}</span>
        <span>{fmtCost(data.costUsd)}</span>
      </div>
      {/* Same tooltip pattern */}
      <div className="absolute left-full top-0 ml-3 z-50 hidden group-hover:block
                      w-72 rounded-xl border border-edge bg-surface-elevated p-4 shadow-2xl text-xs">
        {/* same fields as AgentNodeCard tooltip */}
      </div>
    </div>
  );
}

function UserPromptNode({ data }: { data: { request: string } }) {
  return (
    <div className="w-72 rounded-xl border border-blue-500/40 bg-blue-950/40 p-3 shadow">
      <div className="text-xs font-semibold text-blue-300 mb-1">User Prompt</div>
      <div className="text-sm text-slate-200 line-clamp-3">{data.request}</div>
    </div>
  );
}

// Helpers ----------------------------------------------------------------------

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
function fmtCost(n: number): string {
  return n < 0.001 ? "< $0.001" : `$${n.toFixed(3)}`;
}

// Main page --------------------------------------------------------------------

const nodeTypes = { agentNode: AgentNodeCard, metaNode: MetaNodeCard, userPrompt: UserPromptNode };

export default function TraceFlow() {
  const { id } = useParams<{ id: string }>();
  const { data, isLoading, error } = useTraceEvents(id);

  if (isLoading) return <div className="p-8 text-slate-400">Loading trace…</div>;
  if (error || !data) return <div className="p-8 text-rose-400">Failed to load trace.</div>;

  const { nodes, edges } = buildGraph(data);

  // Totals for header
  const totalIn  = data.events.reduce((s, e) => s + e.input_tokens,  0);
  const totalOut = data.events.reduce((s, e) => s + e.output_tokens, 0);
  const totalCost = data.events.reduce((s, e) => s + e.cost_usd, 0);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-4 px-6 py-4 border-b border-edge">
        <Link to={`/runs/${id}`} className="text-slate-400 hover:text-slate-200 text-sm">← Run</Link>
        <h1 className="font-semibold text-slate-100 truncate flex-1">{data.request}</h1>
        <span className="text-xs text-slate-400">
          ↑ {fmtTokens(totalIn)} · ↓ {fmtTokens(totalOut)} · {fmtCost(totalCost)}
        </span>
      </div>

      {/* Legend */}
      <div className="flex gap-4 px-6 py-2 text-xs text-slate-500">
        <span className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded border border-blue-500/60 bg-blue-950/40 inline-block"/>
          User prompt
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded border border-edge bg-surface inline-block"/>
          Agent (user-facing)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded border border-dashed border-slate-600 bg-slate-900/60 inline-block"/>
          Meta-prompt (internal)
        </span>
      </div>

      {/* Flow canvas */}
      <div className="flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background color="#334155" gap={24} />
          <Controls />
        </ReactFlow>
      </div>
    </div>
  );
}
```

#### `buildGraph` function

```typescript
import type { Node, Edge } from "@xyflow/react";
import type { TraceResponse, TraceEvent } from "../api/client";

const STAGE_Y: Record<string, number> = { plan: 140, work: 280, synthesize: 420 };
const META_Y = 560;
const NODE_W = 220;
const META_W = 180;
const H_GAP = 60;

function roleToLabel(role: string): string {
  const map: Record<string, string> = {
    planner: "Task Planner",
    researcher: "Research Specialist",
    developer: "Developer",
    synthesizer: "Synthesizer",
    critic: "Critic",
    "meta/title": "Title",
    "meta/followups": "Follow-ups",
    "meta/tags": "Tags",
  };
  return map[role] ?? role.split("/").pop()!.replace(/_/g, " ")
    .replace(/\b\w/g, c => c.toUpperCase());
}

export function buildGraph(data: TraceResponse): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // User prompt root node
  nodes.push({
    id: "__user__",
    type: "userPrompt",
    position: { x: 0, y: 0 },
    data: { request: data.request },
  });

  const userEvents = data.events.filter(e => e.prompt_type === "user-facing");
  const metaEvents = data.events.filter(e => e.prompt_type === "meta-prompt");

  // Group user-facing events by stage using agent_role heuristic
  // (roles containing "plan" → plan stage; "synth" → synthesize; else → work)
  const stageOf = (role: string): string => {
    if (/plan/.test(role)) return "plan";
    if (/synth/.test(role)) return "synthesize";
    return "work";
  };

  const byStage: Record<string, TraceEvent[]> = { plan: [], work: [], synthesize: [] };
  for (const ev of userEvents) byStage[stageOf(ev.agent_role)].push(ev);

  // Position crew nodes
  let synthNodeId: string | null = null;
  const firstPlanNodeIds: string[] = [];

  for (const [stage, evs] of Object.entries(byStage)) {
    const y = STAGE_Y[stage];
    const totalW = evs.length * NODE_W + (evs.length - 1) * H_GAP;
    evs.forEach((ev, i) => {
      const x = -totalW / 2 + i * (NODE_W + H_GAP);
      const nodeId = `crew__${ev.agent_role}`;
      nodes.push({
        id: nodeId,
        type: "agentNode",
        position: { x, y },
        data: {
          label: roleToLabel(ev.agent_role),
          model: ev.model ?? "",
          inputTokens: ev.input_tokens,
          outputTokens: ev.output_tokens,
          costUsd: ev.cost_usd,
          promptPreview: ev.prompt_preview ?? "",
          responsePreview: ev.response_preview ?? "",
          toolsUsed: ev.tools_used,
          durationMs: ev.duration_ms ?? 0,
        },
      });
      if (stage === "plan") firstPlanNodeIds.push(nodeId);
      if (stage === "synthesize") synthNodeId = nodeId;
    });
  }

  // Edges: user → first plan nodes
  for (const planId of firstPlanNodeIds) {
    edges.push({ id: `e__user__${planId}`, source: "__user__", target: planId, type: "smoothstep" });
  }

  // Edges within crew from routing.dag
  // dag: { agent_id: [upstream_agent_ids] } — edge goes upstream → agent
  if (data.routing?.dag) {
    for (const [agentId, upstreams] of Object.entries(data.routing.dag)) {
      const targetId = `crew__${agentId}`;
      for (const up of upstreams) {
        const sourceId = `crew__${up}`;
        edges.push({ id: `e__${sourceId}__${targetId}`, source: sourceId, target: targetId, type: "smoothstep" });
      }
    }
  } else {
    // No routing: connect stages linearly
    const stageOrder = ["plan", "work", "synthesize"];
    for (let si = 0; si < stageOrder.length - 1; si++) {
      const from = byStage[stageOrder[si]];
      const to = byStage[stageOrder[si + 1]];
      for (const f of from) for (const t of to)
        edges.push({ id: `e__${f.agent_role}__${t.agent_role}`,
          source: `crew__${f.agent_role}`, target: `crew__${t.agent_role}`, type: "smoothstep" });
    }
  }

  // Meta nodes
  const totalMetaW = metaEvents.length * META_W + (metaEvents.length - 1) * H_GAP;
  metaEvents.forEach((ev, i) => {
    const x = -totalMetaW / 2 + i * (META_W + H_GAP);
    const nodeId = `meta__${ev.agent_role}`;
    nodes.push({
      id: nodeId,
      type: "metaNode",
      position: { x, y: META_Y },
      data: {
        label: roleToLabel(ev.agent_role),
        model: ev.model ?? "",
        inputTokens: ev.input_tokens,
        outputTokens: ev.output_tokens,
        costUsd: ev.cost_usd,
        promptPreview: ev.prompt_preview ?? "",
        responsePreview: ev.response_preview ?? "",
        toolsUsed: ev.tools_used,
        durationMs: ev.duration_ms ?? 0,
      },
    });
    if (synthNodeId) {
      edges.push({ id: `e__synth__${nodeId}`, source: synthNodeId, target: nodeId,
        type: "smoothstep", style: { strokeDasharray: "4 4", stroke: "#64748b" } });
    }
  });

  return { nodes, edges };
}
```

---

### Part 8 — Wire up routes and entry points

**`App.tsx`** — add route:
```tsx
<Route path="runs/:id/trace" element={<TraceFlow />} />
```

**`RunDetail.tsx`** — add a "View Trace" button near the page header (alongside the
existing Langfuse link if present):
```tsx
<Link
  to={`/runs/${taskId}/trace`}
  className="btn-secondary text-sm"
>
  View Trace
</Link>
```

---

### Part 9 — Surface trace URL in OWUI

**File:** `skills/tools/hyperion_owui_tool.py`

In the `status()` method, append the trace URL to the returned string:

```python
def status(self, task_id: str) -> str:
    """Get a task's status, routing, and any pending affordance."""
    data = self._get(f"/tasks/{task_id}")
    lines = [f"status: {data['status']}"]
    # ... existing lines ...
    lines.append(f"\n🔍 Trace: http://localhost:4102/runs/{task_id}/trace")
    return "\n".join(lines)
```

---

## Implementation Order

1. **Part 1** — Add `trace_events` table to `_migrate()` in `api.py`
2. **Part 2** — Extend `HyperionUsageLogger.log_success_event` in `usage.py`
3. **Part 3** — Build `meta_tasks.py` + wire into `runner.py` + disable OWUI built-ins
4. **Part 4** — Add `GET /tasks/{task_id}/trace` to `api.py`
5. **Part 5** — `npm install @xyflow/react` in `hyperion-ui/`
6. **Part 6** — Add types + `useTraceEvents` hook to `client.ts`
7. **Part 7** — Create `TraceFlow.tsx` with `buildGraph` helper
8. **Part 8** — Add route to `App.tsx`, add "View Trace" button to `RunDetail.tsx`
9. **Part 9** — Append trace URL to `hyperion_owui_tool.py` `status()`

## Acceptance Criteria

- `GET /tasks/{task_id}/trace` returns `events` with correct `prompt_type` values
- After a completed run, meta-tasks fire and produce `tasks/{id}/meta/*.txt` files
- Meta-task trace events appear in `trace_events` with `prompt_type="meta-prompt"`
- The Trace Flow page loads at `http://localhost:4102/runs/{task_id}/trace`
- Crew agent nodes appear in stage-order with correct labels, models, tokens, cost
- Meta nodes appear in a separate row with distinct (dashed/grey) styling
- Hovering any node shows tooltip with prompt preview, response preview, tools used
- `status()` in the OWUI tool appends a clickable trace URL
