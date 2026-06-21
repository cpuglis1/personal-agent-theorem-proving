/**
 * Monitoring.tsx — Hyperion UI "Monitoring" page.
 *
 * Role in the system:
 *   Top-level route component for the Hyperion web console (Vite/React/TS app at
 *   :4102). Provides an operational dashboard over the Hyperion orchestrator
 *   (FastAPI :4100): per-agent health/usage metrics plus a paginated table of
 *   recent runs. Designed for at-a-glance triage of agent errors and token-cap
 *   pressure.
 *
 * Data sources:
 *   - useMetrics()  → aggregate run counts (by status), and per-agent metrics
 *                     (activations, errors, error rate, token usage, thresholds).
 *   - useTasks(limit, offset) → paginated list of recent runs with status and
 *                     optional Langfuse trace links.
 *   Both hooks are polling SWR/react-query-style hooks defined in ../api/client;
 *   this component re-renders as fresh data arrives.
 *
 * Key design decisions / non-obvious context:
 *   - Toast alerting fires only on *state transitions*, not on every poll. We
 *     track previously-seen failures and token-cap hits in refs (not state) so
 *     mutating the "seen" set never triggers a re-render. The failures set is
 *     seeded silently on the first poll so historical failures don't spam toasts
 *     on page load. Cap-hit alerts re-arm once an agent drops back under its cap.
 *   - All LLM-call routing, persistence, and metric computation happen server
 *     side; this file is presentation-only.
 */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  useMetrics,
  useTasks,
  type AgentMetric,
  type TaskListItem,
  type TaskStatus,
} from "../api/client";
import { useToast } from "../components/Toast";

/**
 * Tailwind class fragments (border + text colour) keyed by run status.
 * Applied to the status "pill" so each lifecycle state has a distinct hue.
 * Note: `awaiting_approval` and `awaiting_input` deliberately share the amber
 * styling since both represent a paused run waiting on the user.
 */
const STATUS_STYLES: Record<TaskStatus, string> = {
  queued: "border-slate-500/40 text-slate-300",
  running: "border-sky-500/40 text-sky-300",
  awaiting_approval: "border-amber-500/40 text-amber-300",
  awaiting_input: "border-amber-500/40 text-amber-300",
  done: "border-emerald-500/40 text-emerald-300",
  failed: "border-rose-500/40 text-rose-300",
};

/**
 * Format a number with locale-aware thousands separators (e.g. 12345 → "12,345").
 * @param n Raw count (typically a token count).
 * @returns Grouped string for display.
 */
function fmt(n: number): string {
  return n.toLocaleString();
}

/**
 * Format a 0..1 ratio as a whole-number percentage string (e.g. 0.123 → "12%").
 * @param rate Fractional rate in the range [0, 1].
 * @returns Rounded percentage with a trailing "%".
 */
function pct(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

function fmtCost(cost: number): string {
  if (cost > 0 && cost < 0.001) return "< $0.001";
  return `$${cost.toFixed(3)}`;
}

/**
 * Horizontal usage meter for a single token budget.
 *
 * Renders a coloured progress bar plus a "used / cap" caption. When no cap is
 * set the bar is omitted and an unbounded ("used / ∞") caption is shown instead.
 * Bar colour signals pressure: rose at/over cap, amber above 80%, sky otherwise.
 * The fill ratio is clamped to 1 so an over-cap value never overflows the track.
 *
 * @param props.used Tokens consumed so far.
 * @param props.cap  Token cap, or null when the agent has no configured limit.
 */
function UsageBar({ used, cap }: { used: number; cap: number | null }) {
  if (!cap) {
    return <div className="text-xs text-slate-500">{fmt(used)} / ∞</div>;
  }
  const ratio = Math.min(used / cap, 1);
  const over = used >= cap;
  return (
    <div>
      <div className="h-1.5 w-full overflow-hidden rounded bg-edge">
        <div
          className={`h-full rounded ${over ? "bg-rose-400" : ratio > 0.8 ? "bg-amber-400" : "bg-sky-400"}`}
          style={{ width: `${ratio * 100}%` }}
        />
      </div>
      <div className="mt-0.5 text-xs text-slate-500">
        {fmt(used)} / {fmt(cap)}
      </div>
    </div>
  );
}

/**
 * Card summarising one agent's health and resource usage.
 *
 * Shows the agent name/stage, an active/off badge, headline counters (runs,
 * errors, error rate), and input/output token usage bars driven by the agent's
 * configured thresholds. Error rate is tinted rose when non-zero to draw the eye.
 *
 * @param props.a Per-agent metrics object from useMetrics().
 */
function AgentTile({ a }: { a: AgentMetric }) {
  return (
    <div className="card space-y-2">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-medium">{a.name}</div>
          <div className="text-xs text-slate-500">{a.group}</div>
        </div>
        <span
          className={`pill ${a.active ? "border-emerald-500/40 text-emerald-300" : "border-edge text-slate-500"}`}
        >
          {a.active ? "active" : "off"}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 text-sm">
        <div>
          <div className="text-xs text-slate-500">runs</div>
          <div className="font-mono">{a.activations}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">errors</div>
          <div className="font-mono">{a.errors}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">err rate</div>
          <div className={`font-mono ${a.error_rate > 0 ? "text-rose-300" : ""}`}>
            {pct(a.error_rate)}
          </div>
        </div>
      </div>

      <div>
        <div className="text-xs text-slate-500">cost</div>
        <div className="font-mono text-sm text-emerald-300">
          {fmtCost(a.tokens.cost_usd ?? 0)}
        </div>
      </div>

      <div className="space-y-1.5">
        <div>
          <div className="text-xs text-slate-500">input tokens</div>
          <UsageBar used={a.tokens.input} cap={a.thresholds.max_input_tokens} />
        </div>
        <div>
          <div className="text-xs text-slate-500">output tokens</div>
          <UsageBar used={a.tokens.output} cap={a.thresholds.max_output_tokens} />
        </div>
      </div>
    </div>
  );
}

/**
 * Single table row for one run in the "Recent runs" table.
 *
 * Columns: short (8-char) task id linking to the run detail page, the truncated
 * request text (full text on hover via title attr), a status pill, the localised
 * creation timestamp, and an external Langfuse trace link when available
 * (em dash placeholder otherwise).
 *
 * @param props.t Run summary from useTasks().
 */
function RunRow({ t }: { t: TaskListItem }) {
  return (
    <tr className="border-b border-edge/60 last:border-0">
      <td className="py-2 pr-3">
        <Link to={`/runs/${t.task_id}`} className="font-mono text-xs text-sky-300 hover:underline">
          {t.task_id.slice(0, 8)}
        </Link>
      </td>
      <td className="max-w-[320px] truncate py-2 pr-3 text-sm" title={t.request}>
        {t.request}
      </td>
      <td className="py-2 pr-3">
        <span className={`pill ${STATUS_STYLES[t.status]}`}>{t.status}</span>
      </td>
      <td className="py-2 pr-3 text-xs text-slate-500">
        {new Date(t.created_at).toLocaleString()}
      </td>
      <td className="py-2 text-xs">
        {t.langfuse_url ? (
          <a
            href={t.langfuse_url}
            target="_blank"
            rel="noreferrer"
            className="text-sky-300 hover:underline"
          >
            trace ↗
          </a>
        ) : (
          <span className="text-slate-600">—</span>
        )}
      </td>
    </tr>
  );
}

/**
 * Monitoring page component (default route export).
 *
 * Composes the agent metric tiles and a paginated recent-runs table, and wires
 * up transition-based toast alerts for new failures and token-cap hits. State:
 *   - offset: current pagination offset into the runs list (page size = limit).
 *   - seenFailures / seenCapHits: refs tracking already-alerted entities so we
 *     toast only once per transition (see file header for the seeding rationale).
 *
 * @returns The full monitoring dashboard view.
 */
export default function Monitoring() {
  const [offset, setOffset] = useState(0);
  // Fixed page size for the recent-runs table; drives pagination math below.
  const limit = 25;
  const { data: tasks } = useTasks(limit, offset);
  const { data: metrics } = useMetrics();
  const toast = useToast();

  // Toast on newly-observed failed runs and agents that have hit a token cap.
  // A ref-based "seen" set means we alert only on the *transition*, not every poll;
  // the first poll seeds the set silently so we don't shout about historical state.
  const seenFailures = useRef<Set<string> | null>(null);
  const seenCapHits = useRef<Set<string>>(new Set());

  // Alert on runs that newly entered the "failed" state since the last poll.
  useEffect(() => {
    if (!tasks) return;
    const failedNow = tasks.items.filter((t) => t.status === "failed").map((t) => t.task_id);
    if (seenFailures.current === null) {
      seenFailures.current = new Set(failedNow); // seed silently on first load
      return;
    }
    for (const id of failedNow) {
      if (!seenFailures.current.has(id)) {
        seenFailures.current.add(id);
        toast.push(`Run ${id.slice(0, 8)} failed`, "error");
      }
    }
  }, [tasks, toast]);

  // Alert when an agent first reaches/exceeds its input-token cap; re-arm on recovery.
  useEffect(() => {
    if (!metrics) return;
    for (const a of metrics.agents) {
      const cap = a.thresholds.max_input_tokens;
      const key = `${a.id}`;
      if (cap && a.tokens.input >= cap) {
        if (!seenCapHits.current.has(key)) {
          seenCapHits.current.add(key);
          toast.push(`${a.name} hit its input-token cap`, "error");
        }
      } else {
        seenCapHits.current.delete(key); // re-arm once back under the cap
      }
    }
  }, [metrics, toast]);

  // Pagination bounds derived from the server-reported total run count.
  const total = tasks?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + limit < total;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="mb-1 text-lg font-semibold">Monitoring</h2>
        {metrics && (
          <p className="text-sm text-slate-500">
            {metrics.tasks_total} total runs ·{" "}
            {Object.entries(metrics.by_status)
              .map(([s, n]) => `${n} ${s}`)
              .join(" · ")}
          </p>
        )}
      </div>

      <section>
        <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Agents
        </h3>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {(metrics?.agents ?? []).map((a) => (
            <AgentTile key={a.id} a={a} />
          ))}
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Recent runs
        </h3>
        <div className="card overflow-x-auto">
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-edge text-xs uppercase tracking-wide text-slate-500">
                <th className="py-2 pr-3 font-medium">id</th>
                <th className="py-2 pr-3 font-medium">request</th>
                <th className="py-2 pr-3 font-medium">status</th>
                <th className="py-2 pr-3 font-medium">created</th>
                <th className="py-2 font-medium">trace</th>
              </tr>
            </thead>
            <tbody>
              {(tasks?.items ?? []).map((t) => (
                <RunRow key={t.task_id} t={t} />
              ))}
            </tbody>
          </table>
          {tasks && tasks.items.length === 0 && (
            <p className="py-4 text-sm text-slate-500">No runs yet.</p>
          )}
        </div>
        <div className="mt-3 flex items-center gap-3 text-sm">
          <button
            className="btn"
            disabled={!hasPrev}
            onClick={() => setOffset((o) => Math.max(0, o - limit))}
          >
            ← Prev
          </button>
          <span className="text-slate-500">
            {total === 0 ? 0 : offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <button className="btn" disabled={!hasNext} onClick={() => setOffset((o) => o + limit)}>
            Next →
          </button>
        </div>
      </section>
    </div>
  );
}
