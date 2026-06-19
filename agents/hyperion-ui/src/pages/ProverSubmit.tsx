/**
 * ProverSubmit — submit a theorem to the backend with the "lean-prove" workflow.
 *
 * Reuses the console's existing useSubmitTask() mutation (POST /tasks). On
 * success it surfaces the returned task_id and links straight to the live Run
 * view at /prover/runs/:id. Requires the backend (:4100) to be up; submission
 * errors are shown inline.
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useSubmitTask } from "../api/client";

const PLACEHOLDER = `theorem add_comm (a b : ℕ) : a + b = b + a := by
  sorry`;

export default function ProverSubmit() {
  const [task, setTask] = useState("");
  const navigate = useNavigate();
  const submit = useSubmitTask();

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = task.trim();
    if (!text) return;
    submit.mutate({ task: text, workflow: "lean-prove" });
  };

  const taskId = submit.data?.task_id;

  return (
    <div className="mx-auto max-w-3xl">
      <nav className="mb-4 flex gap-2 text-sm">
        <Link className="btn" to="/prover">
          Run view
        </Link>
        <Link className="btn btn-primary" to="/prover/submit">
          Submit a theorem
        </Link>
      </nav>

      <form className="card space-y-3" onSubmit={onSubmit}>
        <div>
          <label className="label" htmlFor="theorem">
            Theorem (Lean 4) · workflow <code>lean-prove</code>
          </label>
          <textarea
            id="theorem"
            className="input lean-inline min-h-40 resize-y"
            placeholder={PLACEHOLDER}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            spellCheck={false}
          />
        </div>
        <div className="flex items-center gap-3">
          <button
            className="btn btn-primary"
            type="submit"
            disabled={submit.isPending || !task.trim()}
          >
            {submit.isPending ? "Submitting…" : "Prove it"}
          </button>
          <span className="text-xs text-slate-500">
            POSTs to the backend at :4100 — the Docker stack must be running.
          </span>
        </div>
      </form>

      {submit.isError && (
        <div className="card mt-4 border-rose-500/40 bg-rose-600/10 text-sm text-rose-200">
          <div className="font-semibold">Submission failed.</div>
          <div className="mt-1 text-rose-300/90">
            {submit.error instanceof Error ? submit.error.message : String(submit.error)}
          </div>
        </div>
      )}

      {taskId && (
        <div className="card mt-4 border-emerald-500/30 bg-emerald-600/10">
          <div className="text-sm text-emerald-200">
            Accepted — task_id <code className="font-mono">{taskId}</code>
          </div>
          <button
            className="btn btn-primary mt-2"
            onClick={() => navigate(`/prover/runs/${encodeURIComponent(taskId)}`)}
            type="button"
          >
            Open live Run view →
          </button>
        </div>
      )}
    </div>
  );
}
