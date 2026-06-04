/**
 * WorkflowEditor.tsx
 *
 * Hyperion UI page for creating and editing a workflow DAG (directed acyclic
 * graph of agent nodes). A workflow describes which agents run, in what order,
 * and which prior nodes each node depends on. The Hyperion runner topo-sorts the
 * nodes on their `upstream` edges to decide execution order and fan-in/fan-out.
 *
 * Role in the system:
 * - Rendered for both "new" (route `/workflows/new`, no `:id` param) and "edit"
 *   (route `/workflows/:id`) flows. The presence of the `:id` route param is the
 *   single source of truth for which mode we are in (`isNew`).
 * - Reads/writes workflow records through the React Query hooks in `../api/client`
 *   (`useWorkflow`, `useSaveWorkflow`, `useDeleteWorkflow`, `useDuplicateWorkflow`),
 *   which ultimately hit the Hyperion FastAPI server. Workflow records are
 *   persisted on disk as JSON (see `agents/hyperion/config/workflows/*.json`).
 * - `useAgents` supplies the list of selectable agent ids for each node.
 *
 * Key design decisions / non-obvious context:
 * - The whole record is held in a single local `rec` state object and edited
 *   immutably (spread copies) so React detects changes. The form is fully
 *   controlled; nothing is committed to the server until `onSave`.
 * - `existing.data` is synced into local state via an effect rather than used
 *   directly, so the user's in-progress edits are not clobbered on re-render but
 *   are still seeded once the fetch resolves.
 * - The workflow `id` (slug) is the on-disk filename / selection key and is fixed
 *   after creation, hence the input is disabled when `!isNew`.
 * - A node's `id` is distinct from its `agent` id: the same agent may appear in
 *   multiple nodes, so dependency edges reference node ids.
 * - Upstream edges are stored on the *downstream* node (each node lists the nodes
 *   that must finish before it). Removing a node therefore also strips that node's
 *   id from every other node's `upstream` list to avoid dangling references.
 */
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  useAgents,
  useDeleteWorkflow,
  useDuplicateWorkflow,
  useSaveWorkflow,
  useWorkflow,
  type NodeKind,
  type WorkflowNode,
  type WorkflowRecord,
} from "../api/client";
import InfoTip from "../components/InfoTip";

// The role a node plays in the workflow. Drives the default task instructions
// (when a node has no explicit override) and the human-in-the-loop gate/revise
// flow: planning happens in "plan" nodes, the final report in "synthesize" nodes.
const NODE_KINDS: NodeKind[] = ["plan", "work", "synthesize"];

/**
 * Build an empty workflow record used to seed the form in "new" mode.
 *
 * @returns A fresh {@link WorkflowRecord} with blank identity fields and no nodes.
 */
function blankWorkflow(): WorkflowRecord {
  return { id: "", name: "", description: "", nodes: [] };
}

/**
 * Build an empty workflow node used when the user clicks "+ Add node".
 *
 * Defaults: no id/agent chosen, "work" role, no upstream dependencies (i.e. a
 * starting node), no human-in-the-loop gate, `instruction: null` meaning "use the
 * kind-derived default behavior", and no conditional-firing rule.
 *
 * @returns A fresh {@link WorkflowNode}.
 */
function blankNode(): WorkflowNode {
  return { id: "", agent: "", kind: "work", upstream: [], gate_before: false, instruction: null, when: null };
}

/**
 * Convert a comma-separated task-types string into a node's `when` value.
 *
 * The node-level `when` rule gates conditional firing: a node runs only when the
 * run's planner-classified task_type is in `task_types`. An empty input means "no
 * condition" and is stored as `null` (the node always fires), so the field never
 * round-trips an empty `{ task_types: [] }`.
 *
 * @param v Raw comma-separated input (e.g. "code, mixed").
 * @returns A `{ task_types }` object, or `null` when no non-empty tokens remain.
 */
function parseWhen(v: string): WorkflowNode["when"] {
  const task_types = v.split(",").map((s) => s.trim()).filter(Boolean);
  return task_types.length ? { task_types } : null;
}

/**
 * Page component for authoring a single workflow DAG.
 *
 * Operates in two modes determined by the `:id` route param:
 * - "new" (`isNew === true`): starts from a blank record and POSTs a new workflow.
 * - "edit": loads the existing record, allows save/duplicate/delete.
 *
 * State management: the full record lives in a single `rec` state object that is
 * mutated immutably by the helper closures below; nothing is persisted until the
 * user saves. On a successful save/delete the user is routed back to `/workflows`;
 * on duplicate they are routed to the clone's edit page.
 *
 * @returns The workflow editor form (or a loading placeholder while fetching in
 *   edit mode).
 */
export default function WorkflowEditor() {
  const { id } = useParams();
  // No id => "new" mode. Used throughout to switch behavior (e.g. lock the slug,
  // hide duplicate/delete) and to pick the create-vs-update save mutation.
  const isNew = !id;
  const nav = useNavigate();

  const existing = useWorkflow(id);
  const { data: agents } = useAgents();

  const save = useSaveWorkflow(isNew);
  const del = useDeleteWorkflow();
  const dup = useDuplicateWorkflow();

  // Single working copy of the record; all form edits flow through here.
  const [rec, setRec] = useState<WorkflowRecord>(blankWorkflow());

  // Seed local state once the fetched record arrives (edit mode). Kept in an
  // effect rather than reading `existing.data` directly so in-progress edits are
  // not overwritten on every re-render.
  useEffect(() => {
    if (existing.data) setRec(existing.data);
  }, [existing.data]);

  /**
   * Immutably update a top-level field on the workflow record.
   *
   * @typeParam K - A key of {@link WorkflowRecord}.
   * @param k - The record field to set (e.g. "name", "description").
   * @param v - The new value for that field.
   */
  function set<K extends keyof WorkflowRecord>(k: K, v: WorkflowRecord[K]) {
    setRec((r) => ({ ...r, [k]: v }));
  }
  /**
   * Immutably merge a partial patch into the node at the given index.
   *
   * @param idx - Index of the node within `rec.nodes`.
   * @param patch - Subset of {@link WorkflowNode} fields to overwrite.
   */
  function setNode(idx: number, patch: Partial<WorkflowNode>) {
    setRec((r) => ({
      ...r,
      nodes: r.nodes.map((n, i) => (i === idx ? { ...n, ...patch } : n)),
    }));
  }
  /** Append a fresh blank node to the end of the workflow. */
  function addNode() {
    setRec((r) => ({ ...r, nodes: [...r.nodes, blankNode()] }));
  }
  /**
   * Remove the node at `idx` and clean up any dependency edges referencing it.
   *
   * Because upstream edges are stored on downstream nodes, deleting a node would
   * otherwise leave dangling references; we therefore also strip the removed
   * node's id from every remaining node's `upstream` list.
   *
   * @param idx - Index of the node to remove from `rec.nodes`.
   */
  function removeNode(idx: number) {
    setRec((r) => {
      const gone = r.nodes[idx]?.id; // id of the node being removed (may be empty)
      return {
        ...r,
        nodes: r.nodes
          .filter((_, i) => i !== idx)
          .map((n) => ({ ...n, upstream: n.upstream.filter((u) => u !== gone) })),
      };
    });
  }
  /**
   * Toggle whether `nodeId` is an upstream dependency of the node at `idx`.
   *
   * Adds the edge if absent, removes it if present (checkbox behavior).
   *
   * @param idx - Index of the downstream node being edited.
   * @param nodeId - Id of the candidate upstream node to add/remove.
   */
  function toggleUpstream(idx: number, nodeId: string) {
    setRec((r) => ({
      ...r,
      nodes: r.nodes.map((n, i) => {
        if (i !== idx) return n;
        const has = n.upstream.includes(nodeId);
        return {
          ...n,
          upstream: has ? n.upstream.filter((u) => u !== nodeId) : [...n.upstream, nodeId],
        };
      }),
    }));
  }

  /** Persist the current record (create or update); navigate to the list on success. */
  function onSave() {
    save.mutate(rec, { onSuccess: () => nav("/workflows") });
  }
  /**
   * Delete the current workflow after a confirmation prompt.
   *
   * No-op in "new" mode (nothing persisted yet). Navigates to the list on success.
   */
  function onDelete() {
    if (!id) return;
    if (!confirm(`Delete workflow "${rec.name || rec.id}"?`)) return;
    del.mutate(id, { onSuccess: () => nav("/workflows") });
  }
  /**
   * Clone the current workflow on the server and navigate to the clone's editor.
   *
   * No-op in "new" mode.
   */
  function onDuplicate() {
    if (!id) return;
    dup.mutate(id, { onSuccess: (clone) => nav(`/workflows/${clone.id}`) });
  }

  // While loading an existing record, show a placeholder (skipped in "new" mode).
  if (!isNew && existing.isLoading) return <p className="text-slate-400">Loading…</p>;

  // Ids available in the per-node agent dropdown; tolerant of agents still loading.
  const agentIds = (agents ?? []).map((a) => a.id);

  return (
    <div className="mx-auto max-w-3xl">
      <h2 className="mb-4 text-lg font-semibold">
        {isNew ? "New workflow" : `Edit: ${rec.name || rec.id}`}
      </h2>

      <div className="space-y-4">
        {/* Identity */}
        <div className="card grid grid-cols-2 gap-3">
          <div>
            <label className="label">
              ID (slug)
              <InfoTip text="Unique identifier used to select this workflow per-run and on disk. Lowercase letters, numbers, hyphens and underscores only. Fixed once created." />
            </label>
            <input
              className="input"
              value={rec.id}
              disabled={!isNew}
              onChange={(e) => set("id", e.target.value)}
            />
          </div>
          <div>
            <label className="label">Name</label>
            <input className="input" value={rec.name} onChange={(e) => set("name", e.target.value)} />
          </div>
          <div className="col-span-2">
            <label className="label">Description</label>
            <input
              className="input"
              value={rec.description}
              onChange={(e) => set("description", e.target.value)}
            />
          </div>
        </div>

        {/* Nodes */}
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Nodes
          </h3>
          <button className="btn" onClick={addNode}>
            + Add node
          </button>
        </div>

        {rec.nodes.length === 0 && (
          <p className="text-sm text-slate-500">
            No nodes yet. A workflow needs at least one node. Add a node, pick an agent, and (for
            anything past the first step) select its upstream dependencies.
          </p>
        )}

        {rec.nodes.map((node, idx) => {
          // Candidate upstream dependencies = every node except this one (no self-edges).
          const others = rec.nodes.filter((_, i) => i !== idx);
          return (
            <div key={idx} className="card space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="label">
                    Node id
                    <InfoTip text="Slug unique within this workflow. Distinct from the agent id — the same agent can appear in more than one node." />
                  </label>
                  <input
                    className="input"
                    value={node.id}
                    onChange={(e) => setNode(idx, { id: e.target.value })}
                  />
                </div>
                <div>
                  <label className="label">
                    Agent
                    <InfoTip text="Which agent record runs at this node." />
                  </label>
                  <select
                    className="input"
                    value={node.agent}
                    onChange={(e) => setNode(idx, { agent: e.target.value })}
                  >
                    <option value="">— pick an agent —</option>
                    {agentIds.map((a) => (
                      <option key={a} value={a}>
                        {a}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label">
                    Role
                    <InfoTip text="The role this node plays in the workflow: plan (decompose the request into a plan.md), work (research/execute the steps), or synthesize (write the final report). Drives the default instruction when no override is set, and the human-in-the-loop plan gate." />
                  </label>
                  <select
                    className="input"
                    value={node.kind}
                    onChange={(e) => setNode(idx, { kind: e.target.value as NodeKind })}
                  >
                    {NODE_KINDS.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <label className="label">
                  Upstream (must finish first)
                  <InfoTip text="The nodes this one depends on. Leave empty for a starting node. Select multiple to fan-in. The runner topo-sorts on these edges." />
                </label>
                {others.length === 0 ? (
                  <p className="text-xs text-slate-500">Add another node to set dependencies.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {others.map((o) => (
                      <label
                        key={o.id || "(unnamed)"}
                        className="flex items-center gap-1.5 text-sm"
                      >
                        {/* Disabled until the candidate node has an id, since
                            edges reference node ids (an empty id cannot be a target). */}
                        <input
                          type="checkbox"
                          disabled={!o.id}
                          checked={node.upstream.includes(o.id)}
                          onChange={() => toggleUpstream(idx, o.id)}
                        />
                        {o.id || "(unnamed)"}
                      </label>
                    ))}
                  </div>
                )}
              </div>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={node.gate_before}
                  onChange={(e) => setNode(idx, { gate_before: e.target.checked })}
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
                  className="input min-h-[60px] resize-y"
                  placeholder="(use the agent's default behavior)"
                  value={node.instruction ?? ""}
                  onChange={(e) => setNode(idx, { instruction: e.target.value || null })}
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
                  value={(node.when?.task_types ?? []).join(", ")}
                  onChange={(e) => setNode(idx, { when: parseWhen(e.target.value) })}
                />
              </div>

              <div className="flex justify-end">
                <button className="btn btn-danger" onClick={() => removeNode(idx)}>
                  Remove node
                </button>
              </div>
            </div>
          );
        })}

        {save.isError && (
          <div className="card border-rose-500/40 bg-rose-500/10 text-sm text-rose-200">
            {(save.error as Error).message}
          </div>
        )}

        <div className="flex items-center gap-2">
          <button className="btn btn-primary" onClick={onSave} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button className="btn" onClick={() => nav("/workflows")}>
            Cancel
          </button>
          {!isNew && (
            <>
              <button className="btn" onClick={onDuplicate} disabled={dup.isPending}>
                Duplicate
              </button>
              <button className="btn btn-danger ml-auto" onClick={onDelete} disabled={del.isPending}>
                Delete
              </button>
            </>
          )}
        </div>
        {del.isError && <p className="text-sm text-rose-300">{(del.error as Error).message}</p>}
      </div>
    </div>
  );
}
