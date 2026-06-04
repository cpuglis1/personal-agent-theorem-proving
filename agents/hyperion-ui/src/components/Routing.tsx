/**
 * Routing.tsx — Hyperion run "Routing" summary card.
 *
 * Purpose:
 *   Presentational React component for the Hyperion UI (the React/Vite web
 *   console at :4102). It renders the agent-routing decision that Hyperion's
 *   orchestrator made for a given run: which agents were chosen to participate
 *   ("Selected") and which were deliberately bypassed ("Skipped"), along with
 *   the reason each one was skipped.
 *
 * Role in the system:
 *   Hyperion's runner/planner decides, per run, which agents (planner,
 *   researcher, developer, critic, synthesizer, ...) actually execute. The
 *   resulting decision is returned by the API as a `RoutingResult` (see
 *   ../api/client). This card is typically embedded in the run-detail view to
 *   give the user visibility into that routing decision.
 *
 * Design notes:
 *   - Pure/stateless: no hooks, no data fetching, no side effects. All data
 *     arrives via the `routing` prop; the parent is responsible for fetching.
 *   - Styling relies on shared utility classes (`card`, `label`, `pill`) plus
 *     Tailwind utilities. These class names are load-bearing for appearance.
 *   - The "Skipped" section is conditionally rendered only when there is at
 *     least one skipped agent, to avoid showing an empty block.
 */
import type { RoutingResult } from "../api/client";

/**
 * Routing — card summarizing the agent-routing decision for a Hyperion run.
 *
 * @param props.routing - The routing decision for the run. Expected shape:
 *   - `selected_agents: string[]` — IDs/names of agents chosen to run. When
 *     empty, a muted "none" placeholder is shown instead of pills.
 *   - `skipped: { id: string; reason: string }[]` — agents that were bypassed,
 *     each with a human-readable reason. The whole section is hidden when empty.
 * @returns A "card" element listing selected agents as pills and, if any,
 *   a list of skipped agents with their skip reasons.
 */
export default function Routing({ routing }: { routing: RoutingResult }) {
  return (
    <div className="card">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
        Routing
      </h3>
      <div className="mb-2">
        <span className="label">Selected</span>
        <div className="flex flex-wrap gap-1.5">
          {routing.selected_agents.length ? (
            routing.selected_agents.map((a) => (
              <span key={a} className="pill border-sky-500/40 text-sky-200">
                {a}
              </span>
            ))
          ) : (
            <span className="text-sm text-slate-500">none</span>
          )}
        </div>
      </div>
      {routing.skipped.length > 0 && (
        <div>
          <span className="label">Skipped</span>
          <ul className="space-y-1 text-sm text-slate-400">
            {routing.skipped.map((s) => (
              <li key={s.id}>
                <span className="font-mono text-slate-300">{s.id}</span> — {s.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
