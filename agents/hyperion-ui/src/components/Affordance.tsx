import { useState } from "react";
import type { Affordance, ApproveBody } from "../api/client";

interface Props {
  affordance: Affordance;
  onApprove: (body: ApproveBody) => void;
  onFeedback: (message: string) => void;
  busy?: boolean;
}

export default function AffordanceView({ affordance, onApprove, onFeedback, busy }: Props) {
  const [chosen, setChosen] = useState<string>(affordance.options[0]?.id ?? "");
  const [edits, setEdits] = useState("");
  const [answer, setAnswer] = useState("");

  const isChoice = affordance.type === "choice" || affordance.type === "confirm";
  const isQuestion = affordance.type === "question" || affordance.type === "form";

  return (
    <div className="card border-sky-500/40 bg-sky-500/5">
      <div className="mb-2 flex items-center gap-2">
        <span className="pill border-sky-500/40 text-sky-200">{affordance.type}</span>
        {affordance.agent_id && (
          <span className="text-xs text-slate-500">from {affordance.agent_id}</span>
        )}
      </div>
      <p className="mb-3 font-medium text-slate-100">{affordance.prompt}</p>

      {isChoice && (
        <div className="space-y-3">
          <div className="space-y-1.5">
            {affordance.options.map((o) => (
              <label
                key={o.id}
                className="flex cursor-pointer items-start gap-2 rounded-md border border-edge p-2 text-sm hover:border-sky-500/40"
              >
                <input
                  type="radio"
                  name="opt"
                  className="mt-1"
                  checked={chosen === o.id}
                  onChange={() => setChosen(o.id)}
                />
                <span>
                  <span className="font-medium">{o.label}</span>
                  {o.description && <span className="block text-xs text-slate-400">{o.description}</span>}
                </span>
              </label>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              className="btn btn-primary"
              disabled={busy}
              onClick={() => onApprove({ action: "approve", chosen_option: chosen })}
            >
              Approve
            </button>
            <button
              className="btn btn-danger"
              disabled={busy}
              onClick={() => onApprove({ action: "reject" })}
            >
              Reject
            </button>
          </div>

          <div className="border-t border-edge pt-3">
            <label className="label">Request a revision instead</label>
            <textarea
              className="input min-h-[60px] resize-y"
              placeholder="What should the planner change?"
              value={edits}
              onChange={(e) => setEdits(e.target.value)}
            />
            <button
              className="btn mt-2"
              disabled={busy || !edits.trim()}
              onClick={() => onApprove({ action: "revise", edits })}
            >
              Send revision
            </button>
          </div>
        </div>
      )}

      {isQuestion && (
        <div>
          <textarea
            className="input min-h-[72px] resize-y"
            placeholder="Your answer…"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
          />
          <button
            className="btn btn-primary mt-2"
            disabled={busy || !answer.trim()}
            onClick={() => onFeedback(answer)}
          >
            Answer
          </button>
        </div>
      )}
    </div>
  );
}
