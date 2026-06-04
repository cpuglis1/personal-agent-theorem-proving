/**
 * AgentCard.tsx — Compact summary card for a single Hyperion agent.
 *
 * Role in the system:
 *   Part of the Hyperion UI (React + TypeScript + Vite + Tailwind console, served on :4102),
 *   which is the front-end for the Hyperion multi-agent orchestrator (FastAPI on :4100).
 *   This component renders one agent's at-a-glance metadata (name, id, active state, group,
 *   model alias, tool count, and an optional schedule hint) as a clickable tile. It is typically
 *   rendered in a grid/list on an agents overview page; clicking it navigates to that agent's
 *   detail route (`/agents/:id`).
 *
 * Data source:
 *   Consumes an {@link AgentRecord} (see ../api/client), which mirrors the agent config returned
 *   by the Hyperion API. No data fetching happens here — the parent supplies the record.
 *
 * Styling notes:
 *   Uses project-defined Tailwind utility classes (`card`, `pill`, `border-edge`) defined in the
 *   app's global CSS, plus inline Tailwind color utilities.
 *
 * Note: an agent is a pure persona — it carries no pipeline role or activation rules. Those
 *   belong to the workflow that references it, so this card no longer shows a stage or trigger.
 */
import { Link } from "react-router-dom";
import type { AgentRecord } from "../api/client";

/**
 * Renders a clickable summary tile for a single agent.
 *
 * The entire card is a router `<Link>` to the agent's detail page (`/agents/:id`). It shows:
 *   - name + id (header row)
 *   - an active/inactive status pill (emerald when active, muted otherwise)
 *   - the description, clamped to two lines (`line-clamp-2`)
 *   - a row of metadata pills: group, model alias, tool count, and a schedule hint when the
 *     agent carries a `schedule_cron`.
 *
 * @param props.agent - The agent record to display.
 * @returns The agent card element.
 */
export default function AgentCard({ agent }: { agent: AgentRecord }) {
  return (
    // Whole card is the navigation target; hover lightens the border for affordance.
    <Link to={`/agents/${agent.id}`} className="card block hover:border-sky-500/50">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-semibold text-slate-100">{agent.name}</div>
          <div className="text-xs text-slate-500">{agent.id}</div>
        </div>
        <span
          className={`pill ${agent.active ? "border-emerald-500/40 text-emerald-300" : "border-edge text-slate-500"}`}
        >
          {agent.active ? "active" : "inactive"}
        </span>
      </div>
      <p className="mt-2 line-clamp-2 text-sm text-slate-400">{agent.description}</p>
      <div className="mt-3 flex flex-wrap gap-1.5">
        <span className="pill border-edge text-slate-300">{agent.group}</span>
        <span className="pill border-edge text-slate-300">{agent.model_alias}</span>
        <span className="pill border-edge text-slate-500">{agent.tools.length} tools</span>
        {agent.schedule_cron && (
          <span className="pill border-amber-500/40 text-amber-200">⏱ {agent.schedule_cron}</span>
        )}
      </div>
    </Link>
  );
}
