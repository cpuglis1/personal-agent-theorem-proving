/**
 * Dashboard.tsx — Hyperion UI landing page.
 *
 * This is the home/index route of the Hyperion web console (Vite + React +
 * TypeScript, served on :4102). It backs onto the Hyperion FastAPI
 * orchestrator (:4100) exclusively through the typed React Query hooks in
 * `../api/client` (useAgents / useConfig / useSubmitTask / useWorkflows) — the
 * UI never talks to LiteLLM or any provider directly.
 *
 * The page serves two distinct jobs, top to bottom:
 *   1. A task Launcher (top card) — lets the operator type a free-form task,
 *      pick a workflow (DAG) and a human-in-the-loop (HITL) review mode, submit
 *      it, and get redirected to the live run detail view.
 *   2. An Agent catalog — lists all registered agents, optionally filtered by
 *      group, grouped into sections, with a shortcut to create a new agent.
 *
 * Design notes:
 *  - Data fetching/mutation state (loading/error/pending) comes from React
 *    Query; this component renders those states inline rather than throwing.
 *  - Tailwind utility classes plus a few project-level component classes
 *    (`card`, `input`, `label`, `btn`, `btn-primary`) drive all styling.
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAgents, useConfig, useSubmitTask, useWorkflows } from "../api/client";
import AgentCard from "../components/AgentCard";

/**
 * Launcher — the "Run a task" card at the top of the dashboard.
 *
 * Collects three inputs from the operator and submits them to the Hyperion
 * orchestrator as a new run:
 *  - `task`:     free-text description of what Hyperion should do.
 *  - `workflow`: which workflow DAG to execute; empty string means "use the
 *                server's configured default workflow".
 *  - `hitl`:     human-in-the-loop review mode — "off" (run unattended),
 *                "plan" (pause for plan approval), or "full" (review every step).
 *
 * On a successful submission it navigates to the new run's detail page so the
 * operator can watch it execute.
 *
 * @returns The task launcher card element.
 */
function Launcher() {
  // Free-text task description typed by the operator.
  const [task, setTask] = useState("");
  // HITL review mode; mirrors the three options in the select below.
  const [hitl, setHitl] = useState<"off" | "plan" | "full">("off");
  // How the run's workflow is chosen: "pick" a saved DAG, or "describe" it in
  // plain language for the server to compile into an ad-hoc DAG (req 4.1).
  const [wfMode, setWfMode] = useState<"pick" | "describe">("pick");
  // Selected workflow DAG id; "" sentinel means "use the server default".
  const [workflow, setWorkflow] = useState<string>("");
  // Plain-language workflow description (used when wfMode === "describe").
  const [workflowPrompt, setWorkflowPrompt] = useState<string>("");
  const submit = useSubmitTask();
  const { data: workflows } = useWorkflows();
  const { data: config } = useConfig();
  const nav = useNavigate();

  // Server-configured fallback workflow, shown as a hint on the "Default" option.
  const defaultId = config?.default_workflow;

  /**
   * Submit the current task to the orchestrator and, on success, redirect to
   * the resulting run's detail page.
   *
   * No-ops if the task text is blank/whitespace-only. In "describe" mode (with a
   * non-empty prompt) it sends `workflow_prompt` for the server to compile;
   * otherwise it sends the picked workflow id (or `null` for the server default).
   * Side effects: triggers the submit mutation and navigates on success.
   */
  function run() {
    if (!task.trim()) return;
    const body =
      wfMode === "describe" && workflowPrompt.trim()
        ? { task, hitl, workflow_prompt: workflowPrompt.trim() }
        : { task, hitl, workflow: workflow || null };
    submit.mutate(body, { onSuccess: (r) => nav(`/runs/${r.task_id}`) });
  }

  return (
    <div className="card mb-6">
      <label className="label">Run a task</label>
      <textarea
        className="input min-h-[72px] resize-y"
        placeholder="Describe what you want Hyperion to do…"
        value={task}
        onChange={(e) => setTask(e.target.value)}
      />

      {/* Workflow source: pick a saved DAG, or describe one in plain language. */}
      <div className="mt-3 inline-flex overflow-hidden rounded-md border border-edge text-sm">
        <button
          className={`px-3 py-1 ${wfMode === "pick" ? "bg-sky-600/30 text-sky-200" : "text-slate-400 hover:bg-slate-800"}`}
          onClick={() => setWfMode("pick")}
        >
          Pick a workflow
        </button>
        <button
          className={`px-3 py-1 ${wfMode === "describe" ? "bg-sky-600/30 text-sky-200" : "text-slate-400 hover:bg-slate-800"}`}
          onClick={() => setWfMode("describe")}
        >
          Describe it
        </button>
      </div>

      {wfMode === "describe" && (
        <textarea
          className="input mt-2 min-h-[60px] resize-y"
          placeholder="e.g. research it with the Researcher, have the Critic review, then synthesize a report"
          value={workflowPrompt}
          onChange={(e) => setWorkflowPrompt(e.target.value)}
        />
      )}

      <div className="mt-3 flex items-center gap-3">
        {wfMode === "pick" && (
          <select
            className="input max-w-[220px]"
            value={workflow}
            onChange={(e) => setWorkflow(e.target.value)}
            title="Which workflow (DAG) to run"
          >
            <option value="">
              Default{defaultId ? ` (${defaultId})` : ""}
            </option>
            {(workflows ?? []).map((w) => (
              <option key={w.id} value={w.id}>
                {w.name || w.id}
              </option>
            ))}
          </select>
        )}
        <select
          className="input max-w-[160px]"
          value={hitl}
          onChange={(e) => setHitl(e.target.value as "off" | "plan" | "full")}
        >
          <option value="off">No review</option>
          <option value="plan">Review plan</option>
          <option value="full">Full HITL</option>
        </select>
        <button className="btn btn-primary" onClick={run} disabled={submit.isPending}>
          {submit.isPending ? "Submitting…" : "Run"}
        </button>
        {submit.isError && (
          <span className="text-sm text-rose-300">{(submit.error as Error).message}</span>
        )}
      </div>
    </div>
  );
}

/**
 * Dashboard — default export and the component mounted at the home route.
 *
 * Renders the {@link Launcher} card followed by the agent catalog: a group
 * filter dropdown, a "New agent" link, and the agents themselves rendered as
 * {@link AgentCard}s grouped by their `group` field. Loading and error states
 * for the agent fetch are surfaced inline.
 *
 * @returns The dashboard page element.
 */
export default function Dashboard() {
  const { data: agents, isLoading, isError, error } = useAgents();
  // Currently selected group filter; "all" shows every agent.
  const [filter, setFilter] = useState<string>("all");

  // Distinct, sorted list of group names — drives the filter dropdown options.
  const allGroups = Array.from(new Set((agents ?? []).map((a) => a.group))).sort();
  // Agents that pass the active group filter.
  const visible = (agents ?? []).filter((a) => filter === "all" || a.group === filter);

  // Bucket the visible agents by group so each group renders as its own section.
  // `??=` lazily initializes the array for a group on first sight.
  const groups = visible.reduce<Record<string, typeof agents>>((acc, a) => {
    (acc[a.group] ??= []).push(a);
    return acc;
  }, {});

  return (
    <div>
      <Launcher />

      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">Agents</h2>
        <div className="flex items-center gap-2">
          {/* Only show the group filter when there is more than one group. */}
          {allGroups.length > 1 && (
            <select
              className="input max-w-[160px]"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            >
              <option value="all">All groups</option>
              {allGroups.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          )}
          <Link to="/agents/new" className="btn btn-primary">
            + New agent
          </Link>
        </div>
      </div>

      {isLoading && <p className="text-slate-400">Loading agents…</p>}
      {isError && <p className="text-rose-300">Failed to load agents: {(error as Error).message}</p>}

      {Object.entries(groups).map(([group, list]) => (
        <section key={group} className="mb-6">
          <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
            {group}
          </h3>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {list!.map((a) => (
              <AgentCard key={a.id} agent={a} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
