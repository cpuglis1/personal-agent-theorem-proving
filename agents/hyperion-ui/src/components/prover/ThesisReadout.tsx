/**
 * ThesisReadout — the compact "did the thesis hold?" panel. Across all sub-goals
 * of a run it surfaces the two headline numbers: solved-rate and Path-A
 * (retrieval) win-rate, plus how many proofs were abstracted/generalized and how
 * many sub-goals were a real A-vs-B contest.
 */
import { thesisStats, type Subgoal } from "../../api/prover";

function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="rounded-lg border border-edge bg-ink/40 px-4 py-3">
      <div className="text-2xl font-bold tabular-nums text-slate-100">{value}</div>
      <div className="mt-0.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
        {label}
      </div>
      {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export default function ThesisReadout({
  subgoals,
}: {
  subgoals: Record<string, Subgoal>;
}) {
  const s = thesisStats(subgoals);
  return (
    <section className="card">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">
          Thesis read-out
        </h2>
        <span className="text-xs text-slate-500">
          across {s.total} sub-goal{s.total === 1 ? "" : "s"}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label="Solved-rate"
          value={pct(s.solvedRate)}
          sub={`${s.solved} / ${s.total} discharged`}
        />
        <Stat
          label="Path-A win-rate"
          value={pct(s.pathAWinRate)}
          sub={`retrieval won ${s.pathAWins} of ${s.solved}`}
        />
        <Stat
          label="Abstractions"
          value={`${s.abstractionsFired}`}
          sub="generalized + re-verified"
        />
        <Stat
          label="Real contests"
          value={`${s.realContests}`}
          sub="A vs B, both verified"
        />
      </div>
    </section>
  );
}
