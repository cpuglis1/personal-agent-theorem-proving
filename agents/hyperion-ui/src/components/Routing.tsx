import type { RoutingResult } from "../api/client";

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
