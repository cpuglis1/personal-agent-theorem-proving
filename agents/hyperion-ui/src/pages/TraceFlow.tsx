/**
 * TraceFlow — interactive node-graph visualization of a single Hyperion run's
 * LLM trace.
 *
 * Role in the system:
 *   This page lives in the Hyperion UI (React console at :4102) and renders the
 *   execution of a multi-agent run as the *actual* workflow DAG. It fetches the
 *   run's trace events from the Hyperion API (via `useTraceEvents`) plus the
 *   run's workflow definition (via `useWorkflow`, keyed by `routing.workflow`),
 *   and lays them onto a React Flow canvas: a single "User Prompt" node at the
 *   top, then one node per *workflow node* positioned by a real topological
 *   layering of `routing.dag`, plus dashed "meta-prompt" nodes for internal
 *   housekeeping LLM calls (title / follow-ups / tags). Clicking a node opens a
 *   resizable detail sidebar that breaks the node down into its individual LLM
 *   calls, with prompt/response previews, token counts, cost, and tools used.
 *
 * Key design decisions / non-obvious context:
 *   - One node per WORKFLOW NODE, not per agent and not per LLM call. Trace
 *     events carry both `agent_role` (the agent id) and `node_id` (the workflow
 *     node the call ran under), so events are grouped by `node_id` — this is
 *     exact even when the same agent runs in more than one node. Meta-prompt
 *     calls have no `node_id` and are grouped by `agent_role` into the meta row.
 *   - The graph structure comes from `routing.dag` (node id -> upstream node
 *     ids), the *real* edges the runner executed — not a fabricated stage order.
 *     Node positions come from a longest-path layering of that DAG, so arbitrary
 *     shapes (parallel branches, critique loops, N-step pipelines) render
 *     correctly.
 *   - Node labels/roles come from the run's workflow definition when available;
 *     skipped nodes (in `routing.skipped`) are dimmed with their reason.
 *   - A manual measurement pass (the rAF loop in `Flow`) works around a React
 *     Flow 12.11 bug where the automatic ResizeObserver never measures these
 *     custom nodes, leaving them hidden and edges/fitView permanently broken.
 *   - Events are split into "user-facing" vs "meta-prompt" by `prompt_type`.
 *
 * @module pages/TraceFlow
 */
import { useEffect, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  useNodesInitialized,
  useReactFlow,
  useStoreApi,
  type Node,
  type Edge,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useParams, Link } from "react-router-dom";
import {
  useTraceEvents,
  useWorkflow,
  type TraceResponse,
  type TraceEvent,
  type WorkflowRecord,
} from "../api/client";

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/**
 * Format a token count compactly for badges/labels.
 * Values >= 1000 are shown in thousands with one decimal (e.g. 1500 -> "1.5k");
 * smaller values are shown as-is.
 *
 * @param n - Raw token count.
 * @returns Human-readable token string.
 */
function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
/**
 * Format a USD cost for display. Costs below $0.001 collapse to a "< $0.001"
 * placeholder so sub-tenth-of-a-cent calls don't render as "$0.000".
 *
 * @param n - Cost in US dollars.
 * @returns Human-readable cost string.
 */
function fmtCost(n: number): string {
  return n < 0.001 ? "< $0.001" : `$${n.toFixed(3)}`;
}

/**
 * Friendly display names for known agent roles / meta-prompt steps. Roles not
 * listed here fall back to a derived title-cased label (see `roleToLabel`).
 */
const ROLE_LABELS: Record<string, string> = {
  planner: "Task Planner",
  researcher: "Research Specialist",
  developer: "Developer",
  synthesizer: "Synthesizer",
  critic: "Critic",
  "meta/title": "Title",
  "meta/followups": "Follow-ups",
  "meta/tags": "Tags",
};

/**
 * Resolve an agent role id to a human-readable label.
 * Prefers an explicit entry in `ROLE_LABELS`; otherwise derives one by taking
 * the last path segment of the role (e.g. "meta/title" -> "title"), replacing
 * underscores with spaces, and title-casing each word.
 *
 * @param role - Raw agent_role id from a trace event.
 * @returns Display label for the role.
 */
function roleToLabel(role: string): string {
  return (
    ROLE_LABELS[role] ??
    role
      .split("/")
      .pop()!
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

// ---------------------------------------------------------------------------
// Node data + custom node components
// ---------------------------------------------------------------------------

/**
 * Aggregated data attached to a workflow-node / meta React Flow node. Represents
 * all LLM calls grouped under one node (or one meta role) during the run, rolled
 * up into a single card. Extends `Record<string, unknown>` to satisfy React
 * Flow's node-data constraint.
 */
interface AgentNodeData extends Record<string, unknown> {
  label: string; // primary label — the workflow node id (or the meta role label)
  agentLabel: string; // which agent ran this node (display name)
  kind: string; // node role: plan / work / synthesize (empty for meta)
  model: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  promptPreview: string;
  responsePreview: string;
  toolsUsed: string[];
  durationMs: number;
  callCount: number;
  rawEvents: TraceEvent[];
  skipped: boolean; // node was in the workflow but did not fire this run
  skipReason: string | null;
}

/** Tailwind accent classes per node kind, for the small role pill. */
const KIND_ACCENT: Record<string, string> = {
  plan: "bg-violet-900/50 text-violet-300",
  work: "bg-sky-900/50 text-sky-300",
  synthesize: "bg-emerald-900/50 text-emerald-300",
};

/**
 * Custom React Flow node for a workflow node. Shows the node id, the agent that
 * ran it, a kind pill, and a token/cost summary. Skipped nodes are dimmed and
 * show a "skipped" badge instead of stats. Top/bottom handles let edges attach.
 *
 * @param props - React Flow node props. `data` is cast to `AgentNodeData`;
 *   `selected` toggles the highlighted border/ring styling.
 * @returns The rendered node card.
 */
function AgentNodeCard({ data, selected }: NodeProps) {
  const d = data as AgentNodeData;
  return (
    <div
      className={`w-56 cursor-pointer rounded-xl border bg-panel p-3 shadow-lg transition-colors ${
        d.skipped ? "opacity-50" : ""
      } ${
        selected ? "border-sky-400 ring-1 ring-sky-400/40" : "border-edge hover:border-slate-500"
      }`}
    >
      <Handle type="target" position={Position.Top} className="!bg-slate-500" />
      <div className="mb-1 flex items-center gap-2">
        <span className="truncate text-sm font-semibold text-slate-100">{d.label}</span>
        {d.kind && (
          <span className={`rounded px-1.5 py-0.5 text-[10px] ${KIND_ACCENT[d.kind] ?? "bg-slate-800 text-slate-400"}`}>
            {d.kind}
          </span>
        )}
      </div>
      <div className="mb-2 flex items-center gap-1.5 truncate text-xs text-slate-400">
        <span className="text-sky-400">🤖</span>
        <span className="truncate">{d.agentLabel}</span>
        {d.callCount > 1 && <span className="text-slate-500">· {d.callCount} calls</span>}
      </div>
      {d.skipped ? (
        <div className="text-xs text-amber-300/80">skipped</div>
      ) : (
        <div className="flex gap-3 text-xs text-slate-300">
          <span>↑ {fmtTokens(d.inputTokens)}</span>
          <span>↓ {fmtTokens(d.outputTokens)}</span>
          <span className="text-emerald-400">{fmtCost(d.costUsd)}</span>
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-slate-500" />
    </div>
  );
}

/**
 * Custom React Flow node for an internal "meta-prompt" step (title /
 * follow-ups / tags generation). Rendered smaller and dashed to visually
 * distinguish housekeeping calls from the workflow nodes.
 *
 * @param props - React Flow node props. `data` is cast to `AgentNodeData`;
 *   `selected` toggles highlighted styling.
 * @returns The rendered meta node card.
 */
function MetaNodeCard({ data, selected }: NodeProps) {
  const d = data as AgentNodeData;
  return (
    <div
      className={`w-44 cursor-pointer rounded-xl border border-dashed bg-slate-900/60 p-2.5 shadow transition-colors ${
        selected ? "border-sky-400/60 ring-1 ring-sky-400/20" : "border-slate-600 hover:border-slate-500"
      }`}
    >
      <Handle type="target" position={Position.Top} className="!bg-slate-600" />
      <div className="mb-1 flex items-center gap-1.5">
        <span className="text-sm text-slate-500">⚙</span>
        <span className="truncate text-xs font-medium text-slate-400">{d.label}</span>
      </div>
      <div className="flex gap-2 text-[11px] text-slate-500">
        <span>↑ {fmtTokens(d.inputTokens)}</span>
        <span>↓ {fmtTokens(d.outputTokens)}</span>
        <span>{fmtCost(d.costUsd)}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail sidebar — shown when a node is clicked
// ---------------------------------------------------------------------------

/**
 * Resizable right-hand detail panel for the currently selected node.
 *
 * Renders one of two layouts depending on the selected node's type:
 *   - "userPrompt" node: shows the original run request text.
 *   - node/meta node: shows the agent, kind, model, duration, call count,
 *     token/cost stats, tools used, and a per-call accordion (`CallItem`) of
 *     prompt/response previews. Skipped nodes show their skip reason.
 * The left edge is a drag handle that resizes the panel between 280 and 900px.
 *
 * @param props.nodeId - Id of the selected node to display.
 * @param props.nodes - Current React Flow node list (looked up by id).
 * @param props.onClose - Called to dismiss the sidebar (clears selection).
 * @param props.width - Current sidebar width in pixels (controlled).
 * @param props.onWidthChange - Setter invoked while dragging the resize handle.
 * @returns The sidebar element, or `null` if the node id is not found.
 */
function DetailSidebar({
  nodeId,
  nodes,
  onClose,
  width,
  onWidthChange,
}: {
  nodeId: string;
  nodes: Node[];
  onClose: () => void;
  width: number;
  onWidthChange: (w: number) => void;
}) {
  // Begin a drag-to-resize interaction. Captures the starting cursor x and
  // width, then tracks mousemove on the document so the drag continues even when
  // the cursor leaves the thin handle. Listeners are torn down on mouseup.
  const handleDragStart = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = width;
    const onMouseMove = (me: MouseEvent) => {
      // dragging left (startX > me.clientX) widens the sidebar
      const next = Math.max(280, Math.min(900, startWidth + (startX - me.clientX)));
      onWidthChange(next);
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  };
  const node = nodes.find((n) => n.id === nodeId);
  if (!node) return null;

  // User prompt node: distinct data shape ({ request }) and a simpler layout.
  if (node.type === "userPrompt") {
    const request = (node.data as { request: string }).request;
    return (
      <aside
        style={{ width }}
        className="relative flex h-full shrink-0 flex-col overflow-hidden border-l border-edge bg-panel/80 backdrop-blur"
      >
        <div
          className="absolute inset-y-0 left-0 w-1 cursor-col-resize hover:bg-sky-500/50 active:bg-sky-400/70"
          onMouseDown={handleDragStart}
        />
        <SidebarHeader title="User Prompt" onClose={onClose} />
        <div className="flex-1 overflow-y-auto p-4 text-sm text-slate-200">
          <p className="whitespace-pre-wrap leading-relaxed">{request}</p>
        </div>
      </aside>
    );
  }

  const d = node.data as AgentNodeData;
  return (
    <aside
      style={{ width }}
      className="relative flex h-full shrink-0 flex-col overflow-hidden border-l border-edge bg-panel/80 backdrop-blur"
    >
      <div
        className="absolute inset-y-0 left-0 w-1 cursor-col-resize hover:bg-sky-500/50 active:bg-sky-400/70"
        onMouseDown={handleDragStart}
      />
      <SidebarHeader title={d.label} onClose={onClose} />
      <div className="flex-1 overflow-y-auto p-4 text-sm">
        {/* Identity row: agent + kind */}
        <div className="mb-3 text-xs text-slate-400">
          {d.agentLabel}
          {d.kind && <span className="ml-2 text-slate-500">· {d.kind}</span>}
        </div>

        {d.skipped ? (
          <div className="card border-amber-500/30 bg-amber-500/5 text-xs text-amber-200">
            This node was skipped this run.
            {d.skipReason && <div className="mt-1 text-amber-300/80">Reason: {d.skipReason}</div>}
          </div>
        ) : (
          <>
            {/* Meta row */}
            <div className="mb-4 flex flex-wrap gap-2 text-xs">
              <span className="rounded bg-slate-800 px-2 py-1 text-slate-300">{d.model || "—"}</span>
              <span className="rounded bg-slate-800 px-2 py-1 text-slate-300">{d.durationMs}ms</span>
              {d.callCount > 1 && (
                <span className="rounded bg-slate-800 px-2 py-1 text-slate-300">
                  {d.callCount} LLM calls
                </span>
              )}
            </div>

            {/* Token / cost row */}
            <div className="mb-4 flex gap-4 text-xs">
              <div>
                <div className="mb-0.5 text-slate-500">Input tokens</div>
                <div className="text-slate-200">{d.inputTokens.toLocaleString()}</div>
              </div>
              <div>
                <div className="mb-0.5 text-slate-500">Output tokens</div>
                <div className="text-slate-200">{d.outputTokens.toLocaleString()}</div>
              </div>
              <div>
                <div className="mb-0.5 text-slate-500">Cost</div>
                <div className="text-emerald-400">{fmtCost(d.costUsd)}</div>
              </div>
            </div>

            {/* Tools used */}
            {d.toolsUsed.length > 0 && (
              <div className="mb-4">
                <div className="mb-1.5 text-xs text-slate-500">Tools used</div>
                <div className="flex flex-wrap gap-1">
                  {d.toolsUsed.map((t) => (
                    <span
                      key={t}
                      className="rounded bg-violet-900/50 px-1.5 py-0.5 text-xs text-violet-300"
                    >
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Per-call accordion */}
            <div className="mb-1.5 text-xs text-slate-500">
              {d.callCount === 1 ? "LLM call" : `LLM calls (${d.callCount} reasoning steps)`}
            </div>
            <div className="space-y-1.5">
              {d.rawEvents.map((ev, i) => (
                <CallItem key={i} ev={ev} index={i} total={d.rawEvents.length} />
              ))}
            </div>
          </>
        )}
      </div>
    </aside>
  );
}

/**
 * Sticky header row for the detail sidebar: a truncated title plus a close
 * button.
 *
 * @param props.title - Heading text (node label or "User Prompt").
 * @param props.onClose - Called when the close (✕) button is clicked.
 * @returns The header element.
 */
function SidebarHeader({ title, onClose }: { title: string; onClose: () => void }) {
  return (
    <div className="flex shrink-0 items-center justify-between border-b border-edge px-4 py-3">
      <span className="truncate font-semibold text-slate-100">{title}</span>
      <button
        onClick={onClose}
        className="ml-2 shrink-0 rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-200"
        aria-label="Close"
      >
        ✕
      </button>
    </div>
  );
}

/**
 * Single collapsible entry in the detail sidebar's per-call accordion,
 * representing one LLM call (one `TraceEvent`) within a node. Collapsed it shows
 * the call number, tools used, and token/duration summary; expanded it reveals
 * the prompt and response previews.
 *
 * @param props.ev - The trace event for this LLM call.
 * @param props.index - Zero-based position within the node's call list.
 * @param props.total - Total number of calls for the node (used for "last" logic).
 * @returns The accordion item element.
 */
function CallItem({ ev, index, total }: { ev: TraceEvent; index: number; total: number }) {
  // Open the last call by default — that's the one with the final answer.
  const [open, setOpen] = useState(index === total - 1);
  const tools = ev.tools_used ?? [];

  return (
    <div className="rounded border border-edge text-xs">
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-800/50"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="font-medium text-slate-300">Call {index + 1}</span>
        {tools.length > 0 && <span className="truncate text-violet-400">{tools.join(", ")}</span>}
        <span className="ml-auto shrink-0 text-slate-500">
          {fmtTokens(ev.input_tokens)}↑ {fmtTokens(ev.output_tokens)}↓
          {ev.duration_ms != null && ` · ${ev.duration_ms}ms`}
          <span className="ml-1.5 text-slate-600">{open ? "▲" : "▼"}</span>
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-edge px-3 pb-3 pt-2">
          {ev.prompt_preview && (
            <div>
              <div className="mb-1 text-[11px] text-slate-500">Prompt</div>
              <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded bg-black/40 p-2 font-mono text-[11px] leading-relaxed text-slate-300">
                {ev.prompt_preview}
              </pre>
            </div>
          )}
          {ev.response_preview && (
            <div>
              <div className="mb-1 text-[11px] text-slate-500">Response</div>
              <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded bg-black/40 p-2 font-mono text-[11px] leading-relaxed text-slate-300">
                {ev.response_preview}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Custom React Flow node for the run's originating user prompt. Always the
 * single root of the graph; only has a source handle (edges flow downward to
 * the first layer of workflow nodes).
 *
 * @param props - React Flow node props; `data.request` holds the prompt text.
 * @returns The rendered user-prompt node.
 */
function UserPromptNode({ data }: NodeProps) {
  const request = (data as { request: string }).request;
  return (
    <div className="w-72 rounded-xl border border-blue-500/40 bg-blue-950/40 p-3 shadow">
      <div className="mb-1 text-xs font-semibold text-blue-300">User Prompt</div>
      <div className="line-clamp-3 text-sm text-slate-200">{request}</div>
      <Handle type="source" position={Position.Bottom} className="!bg-blue-500" />
    </div>
  );
}

/**
 * React Flow node-type registry mapping each custom node's `type` string to its
 * component. Defined module-level (stable reference) so React Flow doesn't treat
 * it as a new node-type map on every render.
 */
const nodeTypes = {
  agentNode: AgentNodeCard,
  metaNode: MetaNodeCard,
  userPrompt: UserPromptNode,
};

// ---------------------------------------------------------------------------
// Data → graph mapping
// ---------------------------------------------------------------------------

// Layout constants (in React Flow canvas units).
const ROW_Y0 = 160; // y of the first workflow-node layer
const ROW_VGAP = 170; // vertical gap between DAG layers
const NODE_W = 230; // assumed width of a workflow node (for horizontal centering)
const META_W = 180; // assumed width of a meta node
const H_GAP = 60; // horizontal gap between sibling nodes in a layer

/** Stat-only rollup of a node's LLM-call events (no label/identity fields). */
interface NodeStats {
  model: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  promptPreview: string;
  responsePreview: string;
  toolsUsed: string[];
  durationMs: number;
  callCount: number;
  rawEvents: TraceEvent[];
}

/**
 * Roll up a list of LLM-call events (in chronological order) into summed
 * token/cost/duration stats, de-duplicated tools, and the raw events. Identity
 * (label / agent / kind) is attached by the caller, which knows the node.
 *
 * @param evs - Trace events for one node (or meta role); may be empty.
 * @returns The aggregated stats.
 */
function aggregateStats(evs: TraceEvent[]): NodeStats {
  const first = evs[0];
  const last = evs[evs.length - 1];
  const tools = Array.from(new Set(evs.flatMap((e) => e.tools_used)));
  return {
    model: evs.find((e) => e.model)?.model ?? "",
    inputTokens: evs.reduce((s, e) => s + e.input_tokens, 0),
    outputTokens: evs.reduce((s, e) => s + e.output_tokens, 0),
    costUsd: evs.reduce((s, e) => s + e.cost_usd, 0),
    promptPreview: first?.prompt_preview ?? "",
    responsePreview: last?.response_preview ?? "",
    toolsUsed: tools,
    durationMs: evs.reduce((s, e) => s + (e.duration_ms ?? 0), 0),
    callCount: evs.length,
    rawEvents: evs,
  };
}

/**
 * Assign each node a layer index = longest dependency path from a root, using
 * the DAG's upstream edges (`dag[node]` = the node ids that must finish first).
 * Roots (no in-DAG upstreams) get layer 0. Cycles are defended against via a
 * visiting set (a node seen on its own stack resolves to its current depth),
 * though valid workflow DAGs are acyclic.
 *
 * @param nodeIds - All node ids to place.
 * @param dag - Adjacency map: node id -> upstream node ids.
 * @returns Map of node id -> layer index.
 */
function computeLayers(nodeIds: string[], dag: Record<string, string[]>): Record<string, number> {
  const known = new Set(nodeIds);
  const layer: Record<string, number> = {};
  const visiting = new Set<string>();
  function depth(id: string): number {
    if (layer[id] !== undefined) return layer[id];
    if (visiting.has(id)) return 0; // cycle guard
    const ups = (dag[id] ?? []).filter((u) => known.has(u));
    if (ups.length === 0) {
      layer[id] = 0;
      return 0;
    }
    visiting.add(id);
    const d = 1 + Math.max(...ups.map(depth));
    visiting.delete(id);
    layer[id] = d;
    return d;
  }
  for (const id of nodeIds) depth(id);
  return layer;
}

/**
 * Group trace events by a key, preserving first-seen order.
 *
 * @param evs - Trace events.
 * @param keyOf - Maps an event to its grouping key.
 * @returns Array of `{ key, events }` groups in first-appearance order.
 */
function groupBy(
  evs: TraceEvent[],
  keyOf: (e: TraceEvent) => string,
): { key: string; events: TraceEvent[] }[] {
  const order: string[] = [];
  const byKey: Record<string, TraceEvent[]> = {};
  for (const ev of evs) {
    const k = keyOf(ev);
    if (!byKey[k]) {
      byKey[k] = [];
      order.push(k);
    }
    byKey[k].push(ev);
  }
  return order.map((key) => ({ key, events: byKey[key] }));
}

/**
 * Build the React Flow node/edge graph for a trace from the *real* workflow DAG.
 *
 * Pipeline:
 *   1. Add the single root "User Prompt" node.
 *   2. Determine the workflow nodes from `routing.dag` (every node the runner
 *      considered) plus their metadata (agent/kind) from the workflow definition.
 *   3. Group user-facing events by `node_id`; attach stats to each node. Mark
 *      nodes in `routing.skipped` as skipped (dimmed, with reason).
 *   4. Layer the nodes by longest dependency path and lay each layer out as a
 *      horizontally-centered row; edge each node from its real upstreams, and the
 *      user prompt -> every root node.
 *   5. Lay out meta-prompt roles (events with no node_id) in a row below, dashed.
 *
 * Falls back to grouping user-facing events by node_id/agent when no routing DAG
 * is present (older or pre-routing-failure runs).
 *
 * @param data - The trace response (request + events + routing).
 * @param workflow - The run's workflow definition, if it could be fetched.
 * @returns The `{ nodes, edges }` to feed React Flow.
 */
function buildGraph(
  data: TraceResponse,
  workflow: WorkflowRecord | undefined,
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  nodes.push({
    id: "__user__",
    type: "userPrompt",
    position: { x: -NODE_W / 2, y: 0 },
    data: { request: data.request },
  });

  const userEvents = data.events.filter((e) => e.prompt_type === "user-facing");
  const metaEvents = data.events.filter((e) => e.prompt_type === "meta-prompt");

  const dag = data.routing?.dag ?? null;
  const skippedReason: Record<string, string> = {};
  for (const s of data.routing?.skipped ?? []) skippedReason[s.id] = s.reason;

  // Node metadata (agent + kind) from the workflow definition, when available.
  // A subworkflow node has no agent — label it with the child workflow id instead.
  const wfMeta: Record<string, { agent: string; kind: string }> = {};
  for (const n of workflow?.nodes ?? [])
    wfMeta[n.id] = { agent: n.agent ?? (n.workflow ? `↳ ${n.workflow}` : ""), kind: n.kind };

  // Events grouped by the node they ran under.
  const eventsByNode: Record<string, TraceEvent[]> = {};
  for (const ev of userEvents) {
    const key = ev.node_id ?? `agent:${ev.agent_role}`;
    (eventsByNode[key] ??= []).push(ev);
  }

  // The node set: prefer the routing DAG (covers skipped nodes too); otherwise
  // fall back to whatever nodes produced events.
  const nodeIds = dag ? Object.keys(dag) : Object.keys(eventsByNode);
  const effectiveDag: Record<string, string[]> = dag ?? {};
  const layers = computeLayers(nodeIds, effectiveDag);

  // Bucket nodes by layer for horizontal centering.
  const byLayer: Record<number, string[]> = {};
  for (const id of nodeIds) (byLayer[layers[id] ?? 0] ??= []).push(id);

  // Helper: derive the agent that ran a node (workflow def first, else events).
  const agentOf = (nodeId: string): string => {
    if (wfMeta[nodeId]) return roleToLabel(wfMeta[nodeId].agent);
    const evs = eventsByNode[nodeId];
    if (evs && evs.length) return roleToLabel(evs[0].agent_role);
    return nodeId.startsWith("agent:") ? roleToLabel(nodeId.slice(6)) : "—";
  };

  // Resolve a node's events: prefer the exact node_id grouping; for runs traced
  // before node-tagging (events keyed by agent, since node_id was null) fall back
  // to the node's agent when the workflow def maps it uniquely.
  const eventsForNode = (nodeId: string): TraceEvent[] => {
    if (eventsByNode[nodeId]?.length) return eventsByNode[nodeId];
    const agent = wfMeta[nodeId]?.agent;
    if (agent) return eventsByNode[`agent:${agent}`] ?? [];
    return [];
  };

  for (const [layerStr, ids] of Object.entries(byLayer)) {
    const layer = Number(layerStr);
    const y = ROW_Y0 + layer * ROW_VGAP;
    const totalW = ids.length * NODE_W + (ids.length - 1) * H_GAP;
    ids.forEach((nodeId, i) => {
      const x = -totalW / 2 + i * (NODE_W + H_GAP);
      const evs = eventsForNode(nodeId);
      const skipped = nodeId in skippedReason && evs.length === 0;
      nodes.push({
        id: `node__${nodeId}`,
        type: "agentNode",
        position: { x, y },
        data: {
          ...aggregateStats(evs),
          label: nodeId,
          agentLabel: agentOf(nodeId),
          kind: wfMeta[nodeId]?.kind ?? "",
          skipped,
          skipReason: skippedReason[nodeId] ?? null,
        } satisfies AgentNodeData,
      });
    });
  }

  // Edges: user prompt -> every root node (no in-DAG upstream); then each node's
  // real upstream edges.
  const known = new Set(nodeIds);
  for (const nodeId of nodeIds) {
    const ups = (effectiveDag[nodeId] ?? []).filter((u) => known.has(u));
    if (ups.length === 0) {
      edges.push({
        id: `e__user__${nodeId}`,
        source: "__user__",
        target: `node__${nodeId}`,
        type: "smoothstep",
      });
    }
    for (const u of ups) {
      edges.push({
        id: `e__${u}__${nodeId}`,
        source: `node__${u}`,
        target: `node__${nodeId}`,
        type: "smoothstep",
      });
    }
  }

  // Meta-prompt roles in a row below the deepest layer, dashed (no edges).
  const maxLayer = nodeIds.length ? Math.max(...nodeIds.map((id) => layers[id] ?? 0)) : -1;
  const metaY = ROW_Y0 + (maxLayer + 1) * ROW_VGAP;
  const metaGroups = groupBy(metaEvents, (e) => e.agent_role);
  const totalMetaW = metaGroups.length * META_W + (metaGroups.length - 1) * H_GAP;
  metaGroups.forEach((g, i) => {
    const x = -totalMetaW / 2 + i * (META_W + H_GAP);
    nodes.push({
      id: `meta__${g.key}`,
      type: "metaNode",
      position: { x, y: metaY },
      data: {
        ...aggregateStats(g.events),
        label: roleToLabel(g.key),
        agentLabel: roleToLabel(g.key),
        kind: "",
        skipped: false,
        skipReason: null,
      } satisfies AgentNodeData,
    });
  });

  return { nodes, edges };
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/**
 * Inner React Flow canvas + detail sidebar. Must render inside a
 * `<ReactFlowProvider>` (it uses `useReactFlow`/`useStoreApi`).
 *
 * React Flow must own node/edge state via useNodesState/useEdgesState so it can
 * write measured dimensions back. Keyed by task id + workflow id (see the parent's
 * <Flow key=...>) so the graph is rebuilt from scratch when navigating between runs
 * or once the workflow definition resolves.
 *
 * Manages node selection (driving the detail sidebar) and the sidebar width, and
 * runs the measurement/fit-view workaround effects described below.
 *
 * @param props.data - The trace response used to build the initial graph.
 * @param props.workflow - The run's workflow definition (for node labels), if any.
 * @returns The canvas + optional sidebar layout.
 */
function Flow({ data, workflow }: { data: TraceResponse; workflow: WorkflowRecord | undefined }) {
  const initial = buildGraph(data, workflow);
  const [nodes, , onNodesChange] = useNodesState(initial.nodes);
  const [edges, , onEdgesChange] = useEdgesState(initial.edges);
  const nodesInitialized = useNodesInitialized();
  const { fitView } = useReactFlow();
  const store = useStoreApi();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sidebarWidth, setSidebarWidth] = useState(384);

  // React Flow 12.11's automatic ResizeObserver measurement never fires for
  // these custom nodes — they stay visibility:hidden with measured={} and
  // handleBounds=null forever, so no edge ever resolves and fitView never runs.
  // Force a measurement pass against the rendered node wrappers once they're in
  // the DOM (deferred via rAF so the wrappers exist), which populates handle
  // bounds, flips nodesInitialized true, and lets edges + fitView work.
  useEffect(() => {
    let raf = 0;
    let tries = 0;
    const measure = () => {
      const { nodeLookup, updateNodeInternals, domNode, nodesInitialized: ok } =
        store.getState();
      if (ok || tries > 30) return;
      tries += 1;
      if (domNode) {
        const updates = new Map<
          string,
          { id: string; nodeElement: HTMLDivElement; force: boolean }
        >();
        for (const id of nodeLookup.keys()) {
          const el = domNode.querySelector<HTMLDivElement>(
            `.react-flow__node[data-id="${CSS.escape(id)}"]`,
          );
          if (el) updates.set(id, { id, nodeElement: el, force: true });
        }
        if (updates.size) updateNodeInternals(updates);
      }
      raf = requestAnimationFrame(measure);
    };
    raf = requestAnimationFrame(measure);
    return () => cancelAnimationFrame(raf);
  }, [store]);

  // Once nodes have measured dimensions (initialized), fit the whole graph into
  // the viewport with some padding.
  useEffect(() => {
    if (nodesInitialized) fitView({ padding: 0.2 });
  }, [nodesInitialized, fitView]);

  return (
    <div className="flex h-full">
      <div className="flex-1 overflow-hidden">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          fitView
          onNodeClick={(_, node) => setSelectedId(node.id)}
          onPaneClick={() => setSelectedId(null)}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="#334155" gap={24} />
          <Controls />
        </ReactFlow>
      </div>
      {selectedId && (
        <DetailSidebar
          nodeId={selectedId}
          nodes={nodes}
          onClose={() => setSelectedId(null)}
          width={sidebarWidth}
          onWidthChange={setSidebarWidth}
        />
      )}
    </div>
  );
}

/**
 * Page component for the trace-flow route (e.g. /runs/:id/trace).
 *
 * Reads the run id from the URL, fetches its trace via `useTraceEvents` and the
 * run's workflow definition via `useWorkflow` (keyed by `routing.workflow`), and
 * renders loading/error states or the full visualization: a header (back link,
 * request title, run-wide token/cost totals), a legend, and the React Flow graph
 * wrapped in `<ReactFlowProvider>`. The inner `<Flow>` is keyed by run id +
 * workflow id so the graph fully remounts when navigating between runs or once the
 * workflow definition resolves.
 *
 * @returns The trace-flow page element.
 */
export default function TraceFlow() {
  const { id } = useParams<{ id: string }>();
  const { data, isLoading, error } = useTraceEvents(id);
  // Fetch the run's workflow for node labels; idle until the trace (and its
  // routing.workflow) resolve. Calling the hook unconditionally keeps hook order
  // stable across the loading/error early-returns below.
  const { data: workflow } = useWorkflow(data?.routing?.workflow ?? undefined);

  if (isLoading) return <div className="p-8 text-slate-400">Loading trace…</div>;
  if (error || !data)
    return <div className="p-8 text-rose-400">Failed to load trace.</div>;

  const totalIn = data.events.reduce((s, e) => s + e.input_tokens, 0);
  const totalOut = data.events.reduce((s, e) => s + e.output_tokens, 0);
  const totalCost = data.events.reduce((s, e) => s + e.cost_usd, 0);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-4 border-b border-edge px-2 py-3">
        <Link to={`/runs/${id}`} className="text-sm text-slate-400 hover:text-slate-200">
          ← Run
        </Link>
        <h1 className="flex-1 truncate font-semibold text-slate-100">{data.request}</h1>
        <span className="text-xs text-slate-400">
          ↑ {fmtTokens(totalIn)} · ↓ {fmtTokens(totalOut)} · {fmtCost(totalCost)}
        </span>
      </div>

      <div className="flex gap-4 px-2 py-2 text-xs text-slate-500">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded border border-blue-500/60 bg-blue-950/40" />
          User prompt
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded border border-edge bg-panel" />
          Workflow node
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded border border-dashed border-slate-600 bg-slate-900/60" />
          Meta-prompt (internal)
        </span>
      </div>

      <div className="h-[calc(100vh-12rem)] min-h-[400px] overflow-hidden">
        <ReactFlowProvider>
          <Flow key={`${id}:${workflow?.id ?? ""}`} data={data} workflow={workflow} />
        </ReactFlowProvider>
      </div>
    </div>
  );
}
