/**
 * Workflows.tsx — Workflow listing page for the Hyperion UI console.
 *
 * Renders the "/workflows" route: a card grid of all workflow definitions
 * known to the Hyperion orchestrator. Each workflow is a DAG of agent nodes
 * (planner / researcher / developer / critic / synthesizer, etc.) executed in
 * dependency order by the backend crew runner.
 *
 * Role in the system:
 *   - Read-only overview. Selecting a card navigates to "/workflows/:id" for
 *     the detail/edit view; the "+ New workflow" button routes to the editor.
 *   - Data is fetched from the Hyperion API (http://localhost:4100) via the
 *     React Query hooks `useWorkflows` and `useConfig` in ../api/client.
 *   - The current run-time default workflow id comes from the server config
 *     (`config.default_workflow`) and is badged with a "default" pill so the
 *     user can see which workflow runs when none is explicitly chosen.
 *
 * Design notes:
 *   - Each node pill shows the node id, with its underlying agent name surfaced
 *     via the `title` (hover) attribute, and a pause glyph ("⏸") appended when
 *     the node has a `gate_before` flag (i.e. requires human approval before it
 *     executes). This keeps the at-a-glance card compact while still hinting at
 *     gating behavior.
 */
import { Link } from "react-router-dom";
import { useConfig, useWorkflows } from "../api/client";

/**
 * Workflows — top-level route component for the workflow list page.
 *
 * Fetches all workflow definitions and the server config, then renders a
 * responsive card grid. Handles loading and error states inline. Takes no
 * props (it is a routed page component).
 *
 * @returns The workflows list page element.
 */
export default function Workflows() {
  // Workflow DAG definitions plus React Query request state (loading/error).
  // `config` provides `default_workflow`, used to badge the active default.
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

      {/* Card grid: one card per workflow. `?? []` guards the initial
          undefined state before the query resolves. */}
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
            {/* Node pills: id is shown, agent name on hover (title),
                "⏸" appended when the node gates for human approval first. */}
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
