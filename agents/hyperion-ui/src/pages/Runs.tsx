/**
 * Runs.tsx — Hyperion UI "Runs" list page.
 *
 * Renders a paginated table of all Hyperion task runs (one row per orchestration
 * run). Each row links to that run's detail view (`/runs/:id`) and to its trace
 * flow visualization (`/runs/:id/trace`). This is the primary entry point in the
 * Hyperion web console (http://localhost:4102) for browsing past and in-flight runs.
 *
 * Role in the system:
 *   - Data comes from the Hyperion FastAPI backend (http://localhost:4100) via the
 *     `useTasks` hook in `../api/client`, which wraps a paginated tasks endpoint.
 *   - Status values mirror the backend task lifecycle (queued → running →
 *     awaiting_* → done/failed) and are color-coded for at-a-glance scanning.
 *
 * Key design decisions / non-obvious context:
 *   - Pagination is offset-based and managed purely in local component state
 *     (no URL sync); `limit` is a fixed page size of 25.
 *   - `STATUS_STYLES` is keyed by the `TaskStatus` union so adding a new backend
 *     status without a style entry would surface as a TypeScript error here.
 *   - `awaiting_approval` and `awaiting_input` deliberately share the same amber
 *     styling since both represent a run paused on human action.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useTasks, type TaskListItem, type TaskStatus } from "../api/client";

/**
 * Tailwind class fragments (border + text color) applied to the status pill,
 * keyed by task status. Used to color-code each run's lifecycle state.
 * Keyed by the full `TaskStatus` union so a missing entry is a compile-time error.
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
 * Single table row for one task run.
 *
 * Renders the run id (truncated to 8 chars, linking to the run detail page),
 * the original request text (truncated with a full-text tooltip), a color-coded
 * status pill, the localized creation timestamp, and a link to the trace flow view.
 *
 * @param props.t - The task list item to render (id, request, status, created_at).
 * @returns A `<tr>` element for use inside the runs table body.
 */
function RunRow({ t }: { t: TaskListItem }) {
  return (
    <tr className="border-b border-edge/60 last:border-0">
      <td className="py-2 pr-3">
        <Link to={`/runs/${t.task_id}`} className="font-mono text-xs text-sky-300 hover:underline">
          {t.task_id.slice(0, 8)}
        </Link>
      </td>
      <td className="max-w-[360px] truncate py-2 pr-3 text-sm" title={t.request}>
        {t.request}
      </td>
      <td className="py-2 pr-3">
        <span className={`pill ${STATUS_STYLES[t.status]}`}>{t.status}</span>
      </td>
      <td className="py-2 pr-3 text-xs text-slate-500">
        {new Date(t.created_at).toLocaleString()}
      </td>
      <td className="py-2 text-xs">
        <Link to={`/runs/${t.task_id}/trace`} className="text-sky-300 hover:underline">
          trace flow →
        </Link>
      </td>
    </tr>
  );
}

/**
 * Runs page component (default export, mounted at the `/runs` route).
 *
 * Fetches a page of task runs via `useTasks` and renders them in a table with
 * offset-based Prev/Next pagination. Handles loading and empty states.
 *
 * @returns The runs list view.
 */
export default function Runs() {
  // Offset-based pagination state, local to this component (not URL-synced).
  const [offset, setOffset] = useState(0);
  const limit = 25; // Fixed page size.
  const { data: tasks, isLoading } = useTasks(limit, offset);

  // Derive pagination affordances; default total to 0 until data arrives.
  const total = tasks?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + limit < total;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Runs</h2>
        <span className="text-sm text-slate-500">{total} total</span>
      </div>

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
        {isLoading && <p className="py-4 text-sm text-slate-500">Loading runs…</p>}
        {tasks && tasks.items.length === 0 && (
          <p className="py-4 text-sm text-slate-500">No runs yet.</p>
        )}
      </div>

      <div className="flex items-center gap-3 text-sm">
        <button
          className="btn"
          disabled={!hasPrev}
          onClick={() => setOffset((o) => Math.max(0, o - limit))}
        >
          ← Prev
        </button>
        {/* Human-readable range, e.g. "1–25 of 120"; collapses to "0" when empty. */}
        <span className="text-slate-500">
          {total === 0 ? 0 : offset + 1}–{Math.min(offset + limit, total)} of {total}
        </span>
        <button className="btn" disabled={!hasNext} onClick={() => setOffset((o) => o + limit)}>
          Next →
        </button>
      </div>
    </div>
  );
}
