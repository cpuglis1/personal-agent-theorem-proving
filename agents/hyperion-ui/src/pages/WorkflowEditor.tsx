/**
 * WorkflowEditor.tsx — graphical (node-and-arrow) editor for a workflow DAG.
 *
 * A workflow is a directed-acyclic graph of agent nodes: which agents run, in
 * what order, and which prior nodes each depends on. The Hyperion runner
 * topo-sorts the nodes on their `upstream` edges to decide execution order and
 * fan-in/fan-out (nodes sharing the same upstream run as a parallel wave).
 *
 * This page replaces the old form-list editor with an interactive React Flow
 * canvas (the same library the read-only trace viewer uses, see TraceFlow.tsx):
 *   - Each node is a draggable card showing its slug, agent, and role (kind).
 *   - You connect nodes by dragging from a node's bottom handle to another
 *     node's top handle. The arrow points downstream: source → target means
 *     "source must finish before target" (i.e. source ∈ target.upstream).
 *   - Clicking a node opens a right-hand panel to edit all of its properties
 *     (slug, agent, kind, HITL gate, instruction override, conditional firing).
 *   - "+ Add node" drops a new node onto the canvas; Backspace/Delete removes
 *     the selected node(s) or edge(s).
 *
 * Role in the system:
 *   - Rendered for both "new" (`/workflows/new`, no `:id`) and "edit"
 *     (`/workflows/:id`) flows; the presence of `:id` is the single source of
 *     truth for the mode (`isNew`).
 *   - Reads/writes workflow records through the React Query hooks in
 *     `../api/client` (`useWorkflow`, `useSaveWorkflow`, `useDeleteWorkflow`,
 *     `useDuplicateWorkflow`). Records persist on disk as JSON
 *     (`agents/hyperion/config/workflows/*.json`). `useAgents` supplies the
 *     selectable agent ids.
 *
 * Key design decisions / non-obvious context:
 *   - GRAPH IDENTITY VS SLUG: each React Flow node carries a stable internal
 *     `uid` as its RF id; the user-facing node slug lives in `data.slug`. Edges
 *     reference uids, so renaming a node's slug never dangles an edge. On save we
 *     map uid → slug to build each node's `upstream` (which the backend stores as
 *     slugs). On load we do the reverse (resolve upstream slugs → uids).
 *   - EDGE DIRECTION = DEPENDENCY: an RF edge source→target encodes
 *     `target.upstream += source.slug`. Visually arrows flow top→down, matching
 *     the trace viewer's layout (roots at the top).
 *   - CYCLE PREVENTION: `onConnect` rejects any edge that would close a cycle
 *     (the backend also validates this, but we stop it at the UI for clarity).
 *   - POSITION PERSISTENCE: node x/y is saved back to the record's optional
 *     `position` field. Records without positions (older workflows, API-authored
 *     ones) are auto-laid-out by longest-path layering on first open.
 *   - MEASUREMENT WORKAROUND: React Flow 12.11's automatic ResizeObserver never
 *     measures these custom nodes, leaving them hidden and fitView broken. The
 *     rAF loop in `Canvas` forces a measurement pass (same fix as TraceFlow).
 *
 * @module pages/WorkflowEditor
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  MarkerType,
  addEdge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  useStoreApi,
  type Node,
  type Edge,
  type Connection,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  useAgents,
  useDeleteWorkflow,
  useDuplicateWorkflow,
  useSaveWorkflow,
  useWorkflow,
  type NodeKind,
  type NodeWhen,
  type WorkflowNode,
  type WorkflowRecord,
} from "../api/client";
import InfoTip from "../components/InfoTip";
import { useToast } from "../components/Toast";

// The role a node plays in the workflow. Drives the default task instructions
// (when a node has no explicit override) and the human-in-the-loop gate/revise
// flow: planning happens in "plan" nodes, the final report in "synthesize" nodes.
const NODE_KINDS: NodeKind[] = ["plan", "work", "synthesize"];

/** Tailwind accent classes per node kind, for the small role pill (mirrors TraceFlow). */
const KIND_ACCENT: Record<string, string> = {
  plan: "bg-violet-900/50 text-violet-300",
  work: "bg-sky-900/50 text-sky-300",
  synthesize: "bg-emerald-900/50 text-emerald-300",
};

// Auto-layout constants (React Flow canvas units), used only for nodes that have
// no persisted position yet.
const LAYOUT_Y0 = 40; // y of the first (root) layer
const LAYOUT_VGAP = 150; // vertical gap between DAG layers
const LAYOUT_X0 = 40; // x of the leftmost node in a layer
const LAYOUT_HGAP = 260; // horizontal gap between sibling nodes in a layer

// ---------------------------------------------------------------------------
// uid generation — stable React Flow node ids decoupled from the editable slug
// ---------------------------------------------------------------------------

let _uidCounter = 0;
/** Generate a process-unique React Flow node id (distinct from the user slug). */
function uid(): string {
  _uidCounter += 1;
  return `wf_${_uidCounter}_${Math.random().toString(36).slice(2, 7)}`;
}

// ---------------------------------------------------------------------------
// Node data + custom node component
// ---------------------------------------------------------------------------

/**
 * Per-node data carried on each React Flow node. Holds every editable field of a
 * workflow node *except* `upstream` (which is derived from edges) and `position`
 * (which React Flow owns). Extends `Record<string, unknown>` to satisfy React
 * Flow's node-data constraint.
 */
interface WfNodeData extends Record<string, unknown> {
  slug: string;
  agent: string;
  kind: NodeKind;
  gate_before: boolean;
  instruction: string | null;
  when: NodeWhen | null;
  /** True when slug or agent is missing — drives the amber "incomplete" ring. */
  invalid: boolean;
}

/**
 * Custom React Flow node card for a workflow node. Shows the slug, the agent that
 * runs it, a kind pill, and a pause glyph when the node gates for approval.
 * Incomplete nodes (no slug/agent) get an amber ring; the selected node gets a
 * sky ring. Top/bottom handles let edges attach (target on top, source on bottom).
 *
 * @param props - React Flow node props; `data` is cast to {@link WfNodeData},
 *   `selected` toggles the highlighted border.
 * @returns The rendered node card.
 */
function WfNodeCard({ data, selected }: NodeProps) {
  const d = data as WfNodeData;
  return (
    <div
      className={`w-48 rounded-xl border bg-panel p-3 shadow-lg transition-colors ${
        selected
          ? "border-sky-400 ring-1 ring-sky-400/40"
          : d.invalid
            ? "border-amber-500/60 ring-1 ring-amber-500/30"
            : "border-edge hover:border-slate-500"
      }`}
    >
      <Handle type="target" position={Position.Top} className="!h-3 !w-3 !bg-slate-400" />
      <div className="mb-1 flex items-center gap-2">
        <span className="truncate text-sm font-semibold text-slate-100">
          {d.slug || <span className="italic text-amber-400">unnamed</span>}
        </span>
        <span
          className={`ml-auto rounded px-1.5 py-0.5 text-[10px] ${KIND_ACCENT[d.kind] ?? "bg-slate-800 text-slate-400"}`}
        >
          {d.kind}
        </span>
      </div>
      <div className="flex items-center gap-1.5 truncate text-xs text-slate-400">
        <span className="text-sky-400">🤖</span>
        <span className="truncate">
          {d.agent || <span className="italic text-amber-400">pick agent</span>}
        </span>
        {d.gate_before && <span className="ml-auto text-amber-300" title="Pauses for approval">⏸</span>}
      </div>
      <Handle type="source" position={Position.Bottom} className="!h-3 !w-3 !bg-slate-400" />
    </div>
  );
}

/**
 * React Flow node-type registry. Defined at module scope (stable reference) so
 * React Flow doesn't treat it as a new map on every render.
 */
const nodeTypes = { wfNode: WfNodeCard };

// ---------------------------------------------------------------------------
// Record <-> graph conversion + graph helpers
// ---------------------------------------------------------------------------

/**
 * Assign each node a layer index = longest dependency path from a root, using the
 * upstream edges. Roots (no upstream) get layer 0. Cycle-guarded, though valid
 * workflows are acyclic. Used only for auto-layout of position-less nodes.
 *
 * @param ids - All node slugs.
 * @param dag - Map of slug -> upstream slugs.
 * @returns Map of slug -> layer index.
 */
function computeLayers(ids: string[], dag: Record<string, string[]>): Record<string, number> {
  const known = new Set(ids);
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
  for (const id of ids) depth(id);
  return layer;
}

/**
 * Convert a persisted workflow record into React Flow nodes + edges.
 *
 * Each node gets a fresh stable uid; the slug is stored in `data.slug`. Positions
 * come from the record when present, otherwise from a longest-path auto-layout.
 * Edges are built from each node's `upstream` slugs, resolved to source uids.
 *
 * @param rec - The workflow record to load.
 * @returns `{ nodes, edges }` ready for React Flow state.
 */
function recordToFlow(rec: WorkflowRecord): { nodes: Node[]; edges: Edge[] } {
  const slugToUid: Record<string, string> = {};
  const uids = rec.nodes.map(() => uid());
  rec.nodes.forEach((n, i) => {
    if (n.id) slugToUid[n.id] = uids[i];
  });

  // Auto-layout fallback positions (only used where a node has no saved position).
  const dag: Record<string, string[]> = {};
  for (const n of rec.nodes) dag[n.id] = n.upstream;
  const layers = computeLayers(
    rec.nodes.map((n) => n.id),
    dag,
  );
  const idxInLayer: Record<string, number> = {};
  const layerCounts: Record<number, number> = {};
  for (const n of rec.nodes) {
    const l = layers[n.id] ?? 0;
    idxInLayer[n.id] = layerCounts[l] ?? 0;
    layerCounts[l] = (layerCounts[l] ?? 0) + 1;
  }

  const nodes: Node[] = rec.nodes.map((n, i) => {
    const auto = {
      x: LAYOUT_X0 + (idxInLayer[n.id] ?? 0) * LAYOUT_HGAP,
      y: LAYOUT_Y0 + (layers[n.id] ?? 0) * LAYOUT_VGAP,
    };
    return {
      id: uids[i],
      type: "wfNode",
      position: n.position ?? auto,
      data: {
        slug: n.id,
        agent: n.agent,
        kind: n.kind,
        gate_before: n.gate_before,
        instruction: n.instruction,
        when: n.when,
        invalid: !n.id || !n.agent,
      } satisfies WfNodeData,
    };
  });

  const edges: Edge[] = [];
  rec.nodes.forEach((n, i) => {
    for (const up of n.upstream) {
      const src = slugToUid[up];
      if (src) {
        edges.push({
          id: `e_${src}_${uids[i]}`,
          source: src,
          target: uids[i],
          type: "smoothstep",
          markerEnd: { type: MarkerType.ArrowClosed },
        });
      }
    }
  });

  return { nodes, edges };
}

/**
 * Would adding the edge `source → target` create a cycle in the current graph?
 * True if `target` can already reach `source` by following existing edges
 * (downstream direction), since the new edge would then close the loop.
 *
 * @param edges - Current edges.
 * @param source - Proposed edge source uid (the upstream node).
 * @param target - Proposed edge target uid (the downstream node).
 * @returns True if the edge would introduce a cycle.
 */
function wouldCreateCycle(edges: Edge[], source: string, target: string): boolean {
  const adj: Record<string, string[]> = {};
  for (const e of edges) (adj[e.source] ??= []).push(e.target);
  const stack = [target];
  const seen = new Set<string>();
  while (stack.length) {
    const cur = stack.pop()!;
    if (cur === source) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    for (const nxt of adj[cur] ?? []) stack.push(nxt);
  }
  return false;
}

/**
 * Parse a comma-separated task-types string into a node's `when` value, or null
 * when empty (the node always fires). Mirrors the old form editor's behavior.
 *
 * @param v - Raw comma-separated input (e.g. "code, mixed").
 * @returns A `{ task_types }` object, or null when no non-empty tokens remain.
 */
function parseWhen(v: string): NodeWhen | null {
  const task_types = v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return task_types.length ? { task_types } : null;
}

// ---------------------------------------------------------------------------
// Node detail side panel
// ---------------------------------------------------------------------------

/**
 * Right-hand panel for editing the selected node's properties. All edits flow up
 * via `onChange(patch)` (merged into the node's data) so the canvas stays the
 * single source of truth. `onDelete` removes the node and its edges.
 *
 * @param props.node - The selected React Flow node.
 * @param props.agentIds - Selectable agent ids for the agent dropdown.
 * @param props.duplicateSlug - True if this node's slug collides with another's.
 * @param props.onChange - Apply a partial patch to the node's data.
 * @param props.onDelete - Remove this node from the graph.
 * @param props.onClose - Dismiss the panel (clears selection).
 * @returns The side panel element.
 */
function NodePanel({
  node,
  agentIds,
  duplicateSlug,
  onChange,
  onDelete,
  onClose,
}: {
  node: Node;
  agentIds: string[];
  duplicateSlug: boolean;
  onChange: (patch: Partial<WfNodeData>) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const d = node.data as WfNodeData;
  return (
    <aside className="flex h-full w-80 shrink-0 flex-col overflow-y-auto border-l border-edge bg-panel/80 backdrop-blur">
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-4 py-3">
        <span className="truncate font-semibold text-slate-100">Edit node</span>
        <button
          onClick={onClose}
          className="ml-2 shrink-0 rounded p-1 text-slate-500 hover:bg-slate-700 hover:text-slate-200"
          aria-label="Close"
        >
          ✕
        </button>
      </div>

      <div className="space-y-3 p-4">
        <div>
          <label className="label">
            Node id (slug)
            <InfoTip text="Slug unique within this workflow. Distinct from the agent id — the same agent can appear in more than one node. Lowercase letters, numbers, hyphens, underscores." />
          </label>
          <input
            className="input"
            value={d.slug}
            onChange={(e) => onChange({ slug: e.target.value })}
          />
          {!d.slug && <p className="mt-1 text-xs text-amber-400">A node id is required.</p>}
          {duplicateSlug && d.slug && (
            <p className="mt-1 text-xs text-amber-400">Another node already uses this id.</p>
          )}
        </div>

        <div>
          <label className="label">
            Agent
            <InfoTip text="Which agent record runs at this node." />
          </label>
          <select
            className="input"
            value={d.agent}
            onChange={(e) => onChange({ agent: e.target.value })}
          >
            <option value="">— pick an agent —</option>
            {agentIds.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          {!d.agent && <p className="mt-1 text-xs text-amber-400">An agent is required.</p>}
        </div>

        <div>
          <label className="label">
            Role
            <InfoTip text="The role this node plays: plan (decompose the request into plan.md), work (research/execute the steps), or synthesize (write the final report). Drives the default instruction when no override is set, and the human-in-the-loop plan gate." />
          </label>
          <select
            className="input"
            value={d.kind}
            onChange={(e) => onChange({ kind: e.target.value as NodeKind })}
          >
            {NODE_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={d.gate_before}
            onChange={(e) => onChange({ gate_before: e.target.checked })}
          />
          Pause for approval before this node
          <InfoTip text="Human-in-the-loop gate: when the run has HITL enabled, it pauses for your approval before this node executes." />
        </label>

        <div>
          <label className="label">
            Instruction override (optional)
            <InfoTip text="Free-form task description for this node. Overrides the kind-derived default — use it for ad-hoc steps like a critique pass. Leave blank to use the role's normal behavior." />
          </label>
          <textarea
            className="input min-h-[80px] resize-y"
            placeholder="(use the agent's default behavior)"
            value={d.instruction ?? ""}
            onChange={(e) => onChange({ instruction: e.target.value || null })}
          />
        </div>

        <div>
          <label className="label">
            Only run for task types (optional)
            <InfoTip text="Comma-separated task types (e.g. 'code, mixed'). When set, this node runs only if the planner classified the request as one of these types — otherwise it is skipped. Leave blank to always run." />
          </label>
          <input
            className="input"
            placeholder="(always run)"
            value={(d.when?.task_types ?? []).join(", ")}
            onChange={(e) => onChange({ when: parseWhen(e.target.value) })}
          />
        </div>

        <button className="btn btn-danger w-full" onClick={onDelete}>
          Delete node
        </button>
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Canvas (must render inside <ReactFlowProvider>)
// ---------------------------------------------------------------------------

/**
 * The interactive editor canvas: identity fields, toolbar, React Flow graph, and
 * the node detail panel. Owns all graph state (`useNodesState`/`useEdgesState`)
 * plus the workflow identity fields; serializes back to a {@link WorkflowRecord}
 * on save.
 *
 * @param props.initialRecord - Record to seed the canvas (blank in "new" mode).
 * @param props.agentIds - Selectable agent ids for node agent dropdowns.
 * @param props.isNew - Whether we are creating (locks the slug after create,
 *   hides Duplicate/Delete, selects create-vs-update save).
 * @returns The full editor UI.
 */
function Canvas({
  initialRecord,
  agentIds,
  isNew,
}: {
  initialRecord: WorkflowRecord;
  agentIds: string[];
  isNew: boolean;
}) {
  const nav = useNavigate();
  const toast = useToast();
  const save = useSaveWorkflow(isNew);
  const del = useDeleteWorkflow();
  const dup = useDuplicateWorkflow();
  const store = useStoreApi();
  const { fitView, screenToFlowPosition } = useReactFlow();
  const wrapperRef = useRef<HTMLDivElement>(null);

  // Workflow identity (kept separate from graph state).
  const [wfId, setWfId] = useState(initialRecord.id);
  const [name, setName] = useState(initialRecord.name);
  const [description, setDescription] = useState(initialRecord.description);

  // Graph state — React Flow owns nodes + edges; this is the source of truth for
  // structure and positions. Computed once from the initial record.
  const initial = recordToFlow(initialRecord);
  const [nodes, setNodes, onNodesChange] = useNodesState(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initial.edges);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // React Flow 12.11's automatic ResizeObserver never measures these custom
  // nodes, so without a forced measurement pass they stay hidden and fitView
  // never resolves. Same workaround as TraceFlow: re-measure via rAF until the
  // store reports nodesInitialized (or we give up after a bounded number of tries).
  useEffect(() => {
    let raf = 0;
    let tries = 0;
    const measure = () => {
      const { nodeLookup, updateNodeInternals, domNode, nodesInitialized: ok } = store.getState();
      if (ok || tries > 30) {
        if (ok) fitView({ padding: 0.2, duration: 0 });
        return;
      }
      tries += 1;
      if (domNode) {
        const updates = new Map<string, { id: string; nodeElement: HTMLDivElement; force: boolean }>();
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
  }, [store, fitView]);

  // Detect duplicate slugs across the graph so the panel can warn about the
  // currently-selected node colliding with another.
  const slugCounts: Record<string, number> = {};
  for (const n of nodes) {
    const s = (n.data as WfNodeData).slug.trim();
    if (s) slugCounts[s] = (slugCounts[s] ?? 0) + 1;
  }

  /**
   * Add an edge (dependency) after validating it: no self-loops, no duplicates,
   * no cycles. Source is the upstream node; target depends on it.
   */
  const onConnect = useCallback(
    (conn: Connection) => {
      if (!conn.source || !conn.target || conn.source === conn.target) return;
      if (edges.some((e) => e.source === conn.source && e.target === conn.target)) return;
      if (wouldCreateCycle(edges, conn.source, conn.target)) {
        toast.push("That connection would create a cycle.", "error");
        return;
      }
      setEdges((es) =>
        addEdge(
          { ...conn, type: "smoothstep", markerEnd: { type: MarkerType.ArrowClosed } },
          es,
        ),
      );
    },
    [edges, setEdges, toast],
  );

  /** Drop a new, blank node onto the canvas (centered in the current viewport). */
  function addNode() {
    const id = uid();
    const rect = wrapperRef.current?.getBoundingClientRect();
    const pos = rect
      ? screenToFlowPosition({ x: rect.left + rect.width / 2, y: rect.top + 120 })
      : { x: 120, y: 120 };
    const n: Node = {
      id,
      type: "wfNode",
      position: pos,
      data: {
        slug: `node-${nodes.length + 1}`,
        agent: "",
        kind: "work",
        gate_before: false,
        instruction: null,
        when: null,
        invalid: true,
      } satisfies WfNodeData,
    };
    setNodes((ns) => [...ns, n]);
    setSelectedId(id);
  }

  /** Merge a patch into one node's data, recomputing its `invalid` flag. */
  function patchNode(nodeId: string, patch: Partial<WfNodeData>) {
    setNodes((ns) =>
      ns.map((n) => {
        if (n.id !== nodeId) return n;
        const data = { ...(n.data as WfNodeData), ...patch };
        data.invalid = !data.slug || !data.agent;
        return { ...n, data };
      }),
    );
  }

  /** Remove a node and any edges touching it; clear selection if it was selected. */
  function deleteNode(nodeId: string) {
    setNodes((ns) => ns.filter((n) => n.id !== nodeId));
    setEdges((es) => es.filter((e) => e.source !== nodeId && e.target !== nodeId));
    setSelectedId((cur) => (cur === nodeId ? null : cur));
  }

  /**
   * Serialize the current canvas into a {@link WorkflowRecord}: map node uids to
   * slugs, derive each node's `upstream` from incoming edges, and capture
   * positions. Returns null (after toasting) if validation fails.
   */
  function buildRecord(): WorkflowRecord | null {
    const problems: string[] = [];
    if (!wfId.trim()) problems.push("Workflow id is required.");
    if (nodes.length === 0) problems.push("Add at least one node.");

    const uidToSlug: Record<string, string> = {};
    for (const n of nodes) uidToSlug[n.id] = (n.data as WfNodeData).slug.trim();

    const seen = new Set<string>();
    for (const n of nodes) {
      const d = n.data as WfNodeData;
      const slug = d.slug.trim();
      if (!slug) problems.push("Every node needs an id.");
      else if (seen.has(slug)) problems.push(`Duplicate node id "${slug}".`);
      else seen.add(slug);
      if (!d.agent) problems.push(`Node "${slug || "(unnamed)"}" needs an agent.`);
    }

    if (problems.length) {
      toast.push(problems[0], "error");
      return null;
    }

    // Each node's upstream = slugs of nodes that are the SOURCE of an edge
    // terminating at this node's uid.
    const upstreamByUid: Record<string, string[]> = {};
    for (const e of edges) (upstreamByUid[e.target] ??= []).push(uidToSlug[e.source]);

    const recNodes: WorkflowNode[] = nodes.map((n) => {
      const d = n.data as WfNodeData;
      return {
        id: d.slug.trim(),
        agent: d.agent,
        kind: d.kind,
        upstream: (upstreamByUid[n.id] ?? []).filter(Boolean),
        gate_before: d.gate_before,
        instruction: d.instruction,
        when: d.when,
        position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      };
    });

    return { id: wfId.trim(), name: name.trim(), description: description.trim(), nodes: recNodes };
  }

  /** Validate + persist the workflow; navigate to the list on success. */
  function onSave() {
    const rec = buildRecord();
    if (!rec) return;
    save.mutate(rec, {
      onSuccess: () => nav("/workflows"),
      onError: (e) => toast.push((e as Error).message, "error"),
    });
  }

  /** Delete the whole workflow (edit mode only) after confirmation. */
  function onDeleteWorkflow() {
    if (isNew) return;
    if (!confirm(`Delete workflow "${name || wfId}"?`)) return;
    del.mutate(wfId, { onSuccess: () => nav("/workflows") });
  }

  /** Clone the workflow on the server (edit mode only); open the clone's editor. */
  function onDuplicate() {
    if (isNew) return;
    dup.mutate(wfId, { onSuccess: (clone) => nav(`/workflows/${clone.id}`) });
  }

  const selectedNode = nodes.find((n) => n.id === selectedId) ?? null;

  return (
    <div className="flex flex-col gap-4">
      {/* Header: title + actions */}
      <div className="flex items-center gap-2">
        <h2 className="text-lg font-semibold">{isNew ? "New workflow" : `Edit: ${name || wfId}`}</h2>
        <div className="ml-auto flex items-center gap-2">
          <button className="btn" onClick={() => nav("/workflows")}>
            Cancel
          </button>
          {!isNew && (
            <button className="btn" onClick={onDuplicate} disabled={dup.isPending}>
              Duplicate
            </button>
          )}
          {!isNew && (
            <button className="btn btn-danger" onClick={onDeleteWorkflow} disabled={del.isPending}>
              Delete
            </button>
          )}
          <button className="btn btn-primary" onClick={onSave} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {/* Identity */}
      <div className="card grid grid-cols-2 gap-3">
        <div>
          <label className="label">
            ID (slug)
            <InfoTip text="Unique identifier used to select this workflow per-run and on disk. Lowercase letters, numbers, hyphens and underscores only. Fixed once created." />
          </label>
          <input
            className="input"
            value={wfId}
            disabled={!isNew}
            onChange={(e) => setWfId(e.target.value)}
          />
        </div>
        <div>
          <label className="label">Name</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="col-span-2">
          <label className="label">Description</label>
          <input
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <button className="btn" onClick={addNode}>
          + Add node
        </button>
        <p className="text-xs text-slate-500">
          Drag from a node's bottom dot to another node's top dot to connect them (source runs
          first). Click a node to edit it. Select a node or arrow and press Delete to remove it.
        </p>
      </div>

      {/* Canvas + side panel */}
      <div className="flex h-[calc(100vh-22rem)] min-h-[420px] overflow-hidden rounded-xl border border-edge">
        <div ref={wrapperRef} className="flex-1 overflow-hidden">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            nodeTypes={nodeTypes}
            onNodeClick={(_, node) => setSelectedId(node.id)}
            onPaneClick={() => setSelectedId(null)}
            deleteKeyCode={["Backspace", "Delete"]}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#334155" gap={24} />
            <Controls />
            <MiniMap pannable zoomable className="!bg-panel" />
          </ReactFlow>
        </div>
        {selectedNode && (
          <NodePanel
            node={selectedNode}
            agentIds={agentIds}
            duplicateSlug={(slugCounts[(selectedNode.data as WfNodeData).slug.trim()] ?? 0) > 1}
            onChange={(patch) => patchNode(selectedNode.id, patch)}
            onDelete={() => deleteNode(selectedNode.id)}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page wrapper — data loading + provider
// ---------------------------------------------------------------------------

/** Build an empty workflow record used to seed the canvas in "new" mode. */
function blankWorkflow(): WorkflowRecord {
  return { id: "", name: "", description: "", nodes: [] };
}

/**
 * Page component for authoring a single workflow DAG graphically.
 *
 * Determines create-vs-edit from the `:id` route param, loads the existing
 * record (edit) plus the agent list, then mounts the React Flow {@link Canvas}
 * inside a `<ReactFlowProvider>`. The canvas is keyed by the resolved record id
 * so it remounts (re-seeding its graph state) once the fetch resolves.
 *
 * @returns The workflow editor page (or a loading placeholder while fetching).
 */
export default function WorkflowEditor() {
  const { id } = useParams();
  const isNew = !id;
  const existing = useWorkflow(id);
  const { data: agents } = useAgents();

  // In edit mode, wait for the record before mounting the canvas so its initial
  // graph state is seeded correctly (the canvas computes nodes/edges once).
  if (!isNew && existing.isLoading) return <p className="text-slate-400">Loading…</p>;

  const record = (!isNew && existing.data) || blankWorkflow();
  const agentIds = (agents ?? []).map((a) => a.id);

  return (
    <ReactFlowProvider>
      <Canvas
        key={isNew ? "new" : record.id}
        initialRecord={record}
        agentIds={agentIds}
        isNew={isNew}
      />
    </ReactFlowProvider>
  );
}
