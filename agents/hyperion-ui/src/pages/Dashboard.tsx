import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAgents, useConfig, useSubmitTask, useWorkflows } from "../api/client";
import AgentCard from "../components/AgentCard";

function Launcher() {
  const [task, setTask] = useState("");
  const [hitl, setHitl] = useState<"off" | "plan" | "full">("off");
  const [workflow, setWorkflow] = useState<string>("");
  const submit = useSubmitTask();
  const { data: workflows } = useWorkflows();
  const { data: config } = useConfig();
  const nav = useNavigate();

  const defaultId = config?.default_workflow;

  function run() {
    if (!task.trim()) return;
    submit.mutate(
      { task, hitl, workflow: workflow || null },
      { onSuccess: (r) => nav(`/runs/${r.task_id}`) },
    );
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
      <div className="mt-3 flex items-center gap-3">
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

export default function Dashboard() {
  const { data: agents, isLoading, isError, error } = useAgents();
  const [filter, setFilter] = useState<string>("all");

  const allGroups = Array.from(new Set((agents ?? []).map((a) => a.group))).sort();
  const visible = (agents ?? []).filter((a) => filter === "all" || a.group === filter);

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
