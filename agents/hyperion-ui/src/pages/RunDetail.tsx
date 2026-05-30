import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  API_BASE,
  useApproveTask,
  useFeedbackTask,
  useSaveToNotion,
  useTask,
  type TaskStatus,
} from "../api/client";
import AffordanceView from "../components/Affordance";
import Routing from "../components/Routing";
import { useToast } from "../components/Toast";

const TERMINAL: TaskStatus[] = ["done", "failed"];

const statusColor: Record<string, string> = {
  running: "border-sky-500/40 text-sky-200",
  queued: "border-edge text-slate-300",
  awaiting_approval: "border-amber-500/40 text-amber-200",
  awaiting_input: "border-amber-500/40 text-amber-200",
  done: "border-emerald-500/40 text-emerald-200",
  failed: "border-rose-500/40 text-rose-200",
};

function Result({ taskId }: { taskId: string }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/tasks/${taskId}/artifacts/result.md`)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setText)
      .catch((e) => setErr(e.message));
  }, [taskId]);

  if (err) return <p className="text-sm text-slate-500">No result.md ({err})</p>;
  if (text === null) return <p className="text-sm text-slate-500">Loading result…</p>;
  return (
    <pre className="card max-h-[500px] overflow-auto whitespace-pre-wrap text-sm text-slate-200">
      {text}
    </pre>
  );
}

export default function RunDetail() {
  const { id } = useParams();
  const taskId = id!;

  const { data: task } = useTask(taskId, {
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s && TERMINAL.includes(s) ? false : 1500;
    },
  });

  const approve = useApproveTask(taskId);
  const feedback = useFeedbackTask(taskId);
  const busy = approve.isPending || feedback.isPending;

  if (!task) return <p className="text-slate-400">Loading task…</p>;

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Run</h2>
          <div className="font-mono text-xs text-slate-500">{task.task_id}</div>
        </div>
        <span className={`pill ${statusColor[task.status] ?? "border-edge"}`}>{task.status}</span>
      </div>

      {task.error && (
        <div className="card border-rose-500/40 bg-rose-500/10 text-sm text-rose-200">
          {task.error}
        </div>
      )}

      {task.pending_affordance && (
        <AffordanceView
          affordance={task.pending_affordance}
          busy={busy}
          onApprove={(body) => approve.mutate(body)}
          onFeedback={(msg) => feedback.mutate(msg)}
        />
      )}

      {task.status === "running" && (
        <div className="card">
          <label className="label">Steer this run (drained between stages)</label>
          <SteerBox onSend={(m) => feedback.mutate(m)} busy={busy} />
        </div>
      )}

      {task.routing && <Routing routing={task.routing} />}

      <div className="card">
        <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Progress
        </h3>
        <pre className="max-h-[300px] overflow-auto whitespace-pre-wrap text-xs text-slate-400">
          {task.progress_lines.join("\n") || "(no progress yet)"}
        </pre>
      </div>

      {task.status === "done" && (
        <div>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
              Result
            </h3>
            <SaveToNotionButton taskId={taskId} />
          </div>
          <Result taskId={taskId} />
        </div>
      )}
    </div>
  );
}

function SaveToNotionButton({ taskId }: { taskId: string }) {
  const save = useSaveToNotion(taskId);
  const toast = useToast();
  return (
    <button
      className="btn"
      disabled={save.isPending}
      onClick={() =>
        save.mutate(undefined, {
          onSuccess: (r) =>
            toast.push(r.url ? `Saved to Notion → ${r.url}` : "Saved to Notion", "success"),
          onError: (e) => toast.push((e as Error).message, "error"),
        })
      }
    >
      {save.isPending ? "Saving…" : "Save to Notion"}
    </button>
  );
}

function SteerBox({ onSend, busy }: { onSend: (m: string) => void; busy: boolean }) {
  const [msg, setMsg] = useState("");
  return (
    <div className="flex gap-2">
      <input
        className="input"
        placeholder="e.g. focus on the cost section"
        value={msg}
        onChange={(e) => setMsg(e.target.value)}
      />
      <button
        className="btn btn-primary"
        disabled={busy || !msg.trim()}
        onClick={() => {
          onSend(msg);
          setMsg("");
        }}
      >
        Send
      </button>
    </div>
  );
}
