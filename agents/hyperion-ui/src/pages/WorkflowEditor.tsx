import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  useAgents,
  useDeleteWorkflow,
  useDuplicateWorkflow,
  useSaveWorkflow,
  useWorkflow,
  type WorkflowNode,
  type WorkflowRecord,
} from "../api/client";
import InfoTip from "../components/InfoTip";

function blankWorkflow(): WorkflowRecord {
  return { id: "", name: "", description: "", nodes: [] };
}

function blankNode(): WorkflowNode {
  return { id: "", agent: "", upstream: [], gate_before: false, instruction: null };
}

export default function WorkflowEditor() {
  const { id } = useParams();
  const isNew = !id;
  const nav = useNavigate();

  const existing = useWorkflow(id);
  const { data: agents } = useAgents();

  const save = useSaveWorkflow(isNew);
  const del = useDeleteWorkflow();
  const dup = useDuplicateWorkflow();

  const [rec, setRec] = useState<WorkflowRecord>(blankWorkflow());

  useEffect(() => {
    if (existing.data) setRec(existing.data);
  }, [existing.data]);

  function set<K extends keyof WorkflowRecord>(k: K, v: WorkflowRecord[K]) {
    setRec((r) => ({ ...r, [k]: v }));
  }
  function setNode(idx: number, patch: Partial<WorkflowNode>) {
    setRec((r) => ({
      ...r,
      nodes: r.nodes.map((n, i) => (i === idx ? { ...n, ...patch } : n)),
    }));
  }
  function addNode() {
    setRec((r) => ({ ...r, nodes: [...r.nodes, blankNode()] }));
  }
  function removeNode(idx: number) {
    setRec((r) => {
      const gone = r.nodes[idx]?.id;
      return {
        ...r,
        nodes: r.nodes
          .filter((_, i) => i !== idx)
          .map((n) => ({ ...n, upstream: n.upstream.filter((u) => u !== gone) })),
      };
    });
  }
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

  function onSave() {
    save.mutate(rec, { onSuccess: () => nav("/workflows") });
  }
  function onDelete() {
    if (!id) return;
    if (!confirm(`Delete workflow "${rec.name || rec.id}"?`)) return;
    del.mutate(id, { onSuccess: () => nav("/workflows") });
  }
  function onDuplicate() {
    if (!id) return;
    dup.mutate(id, { onSuccess: (clone) => nav(`/workflows/${clone.id}`) });
  }

  if (!isNew && existing.isLoading) return <p className="text-slate-400">Loading…</p>;

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
                  <InfoTip text="Free-form task description for this node. Overrides the agent's stage-derived default — use it for ad-hoc steps like a critique pass. Leave blank to use the agent's normal behavior." />
                </label>
                <textarea
                  className="input min-h-[60px] resize-y"
                  placeholder="(use the agent's default behavior)"
                  value={node.instruction ?? ""}
                  onChange={(e) => setNode(idx, { instruction: e.target.value || null })}
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
