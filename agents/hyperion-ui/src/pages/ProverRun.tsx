/**
 * ProverRun — the Run view (centerpiece) of the prover console.
 *
 * Renders the per-stage, per-sub-goal trace of a Lean proof run. Two data
 * sources behind one toggle:
 *   • Fixture — the bundled sample-trace.json (works with no backend).
 *   • Live    — GET /tasks/{id}/trace from the backend (:4100) for a task_id,
 *               reachable at /prover/runs/:id (or by pasting an id here).
 *
 * Layout: thesis read-out → scaffold → per-sub-goal pipeline cards → result.lean.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import LeanCode from "../components/LeanCode";
import SubgoalCard from "../components/prover/SubgoalCard";
import ThesisReadout from "../components/prover/ThesisReadout";
import { useProverTrace, type TraceSource } from "../api/prover";

function ProverSubNav() {
  return (
    <nav className="mb-4 flex gap-2 text-sm">
      <Link className="btn btn-primary" to="/prover">
        Run view
      </Link>
      <Link className="btn" to="/prover/submit">
        Submit a theorem
      </Link>
    </nav>
  );
}

export default function ProverRun() {
  const { id } = useParams<{ id?: string }>();
  const navigate = useNavigate();

  // Default to live when arriving at /prover/runs/:id, else show the fixture.
  const [source, setSource] = useState<TraceSource>(id ? "live" : "fixture");
  const [taskIdInput, setTaskIdInput] = useState(id ?? "");

  // Keep the source in step with the URL: a real :id means a live run.
  useEffect(() => {
    if (id) {
      setSource("live");
      setTaskIdInput(id);
    }
  }, [id]);

  const { data, isLoading, isError, error } = useProverTrace(source, id);

  const loadLive = () => {
    const t = taskIdInput.trim();
    if (t) navigate(`/prover/runs/${encodeURIComponent(t)}`);
  };

  const prover = data?.prover ?? null;
  const subgoals = prover?.subgoals ?? {};
  const subgoalIds = Object.keys(subgoals);

  return (
    <div className="mx-auto max-w-5xl">
      <ProverSubNav />

      {/* Data-source controls */}
      <div className="card mb-4 flex flex-wrap items-center gap-3">
        <span className="label mb-0">Trace source</span>
        <div className="flex gap-1">
          <button
            className={`btn ${source === "fixture" ? "btn-primary" : ""}`}
            onClick={() => setSource("fixture")}
            type="button"
          >
            Fixture
          </button>
          <button
            className={`btn ${source === "live" ? "btn-primary" : ""}`}
            onClick={() => setSource("live")}
            type="button"
          >
            Live backend trace
          </button>
        </div>
        {source === "live" && (
          <div className="flex flex-1 items-center gap-2">
            <input
              className="input"
              placeholder="paste task_id…"
              value={taskIdInput}
              onChange={(e) => setTaskIdInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && loadLive()}
            />
            <button className="btn btn-primary" onClick={loadLive} type="button">
              Load
            </button>
          </div>
        )}
      </div>

      {/* States */}
      {source === "live" && !id && (
        <div className="card text-sm text-slate-400">
          Enter a task_id above (or submit a theorem) to load GET /tasks/&lt;id&gt;/trace
          from the Hyperion backend at :4100.
        </div>
      )}
      {isLoading && <div className="card text-sm text-slate-400">Loading trace…</div>}
      {isError && (
        <div className="card border-rose-500/40 bg-rose-600/10 text-sm text-rose-200">
          <div className="font-semibold">Couldn’t load the trace.</div>
          <div className="mt-1 text-rose-300/90">
            {error instanceof Error ? error.message : String(error)}
          </div>
        </div>
      )}
      {data && !prover && (
        <div className="card text-sm text-slate-400">
          This task has no prover trace (<code>prover</code> is null) — it isn’t a
          Lean-prove run.
        </div>
      )}

      {/* The run */}
      {prover && data && (
        <div className="space-y-4">
          <section className="card">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <div className="text-xs uppercase tracking-wide text-slate-400">
                  request
                </div>
                <div className="font-mono text-slate-100">{prover.request}</div>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span className="pill">task: {data.task_id}</span>
                <span className={`pill ${prover.status === "done" ? "pill--good" : ""}`}>
                  {prover.status}
                </span>
                <span
                  className={`pill ${
                    prover.skeleton_ok ? "pill--good" : prover.skeleton_ok === false ? "pill--bad" : "pill--muted"
                  }`}
                >
                  skeleton {prover.skeleton_ok ? "ok" : prover.skeleton_ok === false ? "broken" : "?"}
                </span>
              </div>
            </div>
          </section>

          <ThesisReadout subgoals={subgoals} />

          {prover.scaffold && (
            <section className="card">
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-300">
                Decomposer output · scaffold and sub-goals
              </h2>
              <LeanCode code={prover.scaffold} label="decomposed skeleton (sorry placeholders)" />
              {subgoalIds.length > 0 && (
                <div className="mt-3 grid gap-2 text-xs">
                  {subgoalIds.map((sid) => (
                    <div key={sid} className="rounded border border-edge bg-slate-900/40 p-2">
                      <span className="mr-2 font-mono text-sky-300">{sid}</span>
                      <code className="text-slate-300">{subgoals[sid].lean_type}</code>
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}

          <div>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-300">
              Sub-goals · {subgoalIds.length}
            </h2>
            <div className="space-y-4">
              {subgoalIds.map((sid) => (
                <SubgoalCard key={sid} id={sid} sg={subgoals[sid]} />
              ))}
            </div>
          </div>

          {prover.result_lean && (
            <section className="card border-emerald-500/30">
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-emerald-300">
                Final result.lean
              </h2>
              <LeanCode code={prover.result_lean} label="assembled proof" />
            </section>
          )}
        </div>
      )}
    </div>
  );
}
