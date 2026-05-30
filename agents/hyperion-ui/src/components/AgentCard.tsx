import { Link } from "react-router-dom";
import type { AgentRecord } from "../api/client";

const stageColor: Record<string, string> = {
  plan: "border-violet-500/40 text-violet-200",
  work: "border-sky-500/40 text-sky-200",
  synthesize: "border-emerald-500/40 text-emerald-200",
};

function triggerLabel(a: AgentRecord): string {
  const t = a.trigger;
  switch (t.type) {
    case "task_type":
      return `task_type: ${t.task_types.join(", ") || "—"}`;
    case "keyword":
      return `keyword: ${t.keywords.join(", ") || "—"}`;
    case "upstream":
      return `after: ${t.upstream.join(", ") || "—"}`;
    case "schedule":
      return `cron: ${t.cron ?? "—"}`;
    default:
      return "always";
  }
}

export default function AgentCard({ agent }: { agent: AgentRecord }) {
  return (
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
        <span className={`pill ${stageColor[agent.stage] ?? "border-edge"}`}>{agent.stage}</span>
        <span className="pill border-edge text-slate-300">{agent.model_alias}</span>
        <span className="pill border-edge text-slate-400">{triggerLabel(agent)}</span>
        <span className="pill border-edge text-slate-500">{agent.tools.length} tools</span>
      </div>
    </Link>
  );
}
