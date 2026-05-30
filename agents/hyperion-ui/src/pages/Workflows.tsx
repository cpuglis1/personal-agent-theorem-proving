import { Link } from "react-router-dom";
import { useConfig, useWorkflows } from "../api/client";

export default function Workflows() {
  const { data: workflows, isLoading, isError, error } = useWorkflows();
  const { data: config } = useConfig();
  const defaultId = config?.default_workflow;

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">Workflows</h2>
        <Link to="/workflows/new" className="btn btn-primary">
          + New workflow
        </Link>
      </div>

      <p className="mb-4 text-sm text-slate-500">
        A workflow is a DAG of agent nodes run in dependency order. Pick one per run on the
        dashboard, or set a default in Settings.
      </p>

      {isLoading && <p className="text-slate-400">Loading workflows…</p>}
      {isError && (
        <p className="text-rose-300">Failed to load workflows: {(error as Error).message}</p>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {(workflows ?? []).map((w) => (
          <Link
            key={w.id}
            to={`/workflows/${w.id}`}
            className="card block hover:border-sky-500/40"
          >
            <div className="flex items-center justify-between">
              <span className="font-medium">{w.name || w.id}</span>
              {w.id === defaultId && (
                <span className="pill border-sky-500/40 text-sky-300">default</span>
              )}
            </div>
            <div className="mt-1 font-mono text-xs text-slate-500">{w.id}</div>
            {w.description && (
              <p className="mt-2 text-sm text-slate-400">{w.description}</p>
            )}
            <div className="mt-3 flex flex-wrap gap-1.5">
              {w.nodes.map((n) => (
                <span key={n.id} className="pill border-edge text-slate-400" title={n.agent}>
                  {n.id}
                  {n.gate_before ? " ⏸" : ""}
                </span>
              ))}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
