/**
 * Affordance.tsx — Human-in-the-loop interaction widget for the Hyperion console.
 *
 * Role in the system:
 *   During a Hyperion run, agents can pause and surface an "affordance": a request
 *   for human input that gates further progress (e.g. the planner asking the user to
 *   approve a plan, choose between options, or answer a clarifying question). The
 *   Hyperion API (FastAPI :4100) exposes these via its affordances endpoint; the
 *   parent page polls/streams them and renders this component for each pending one.
 *
 *   This component is purely presentational + local form state. It does not call the
 *   API itself — it delegates the user's decision back to the parent through the
 *   `onApprove` / `onFeedback` callbacks, which own the actual network requests.
 *
 * Affordance kinds (driven by `affordance.type`):
 *   - "choice" / "confirm"  -> rendered as a radio list of options plus
 *                              Approve / Reject / Send-revision controls.
 *   - "question" / "form"   -> rendered as a free-text answer box.
 *
 * Key design decisions / non-obvious context:
 *   - Two distinct callbacks exist because the two flows map to different API
 *     semantics: structured approve/reject/revise (ApproveBody) vs. a free-text
 *     feedback message. The component never mixes them.
 *   - The initial radio selection defaults to the first option's id (or "" when
 *     there are no options) so an Approve click always has a sane `chosen_option`.
 *   - All styling is via shared utility classes (card / pill / btn / input / label)
 *     defined in the app's Tailwind layer; this file intentionally carries no CSS.
 */
import { useState } from "react";
import type { Affordance, ApproveBody } from "../api/client";

/**
 * Props for {@link AffordanceView}.
 *
 * @property affordance - The pending affordance to render (type, prompt, options,
 *                        and originating agent_id).
 * @property onApprove  - Called for choice/confirm decisions with a structured
 *                        body: approve (+ chosen_option), reject, or revise (+ edits).
 * @property onFeedback - Called for question/form affordances with the user's
 *                        free-text answer.
 * @property busy       - When true, disables all action controls to prevent
 *                        double-submission while a request is in flight.
 */
interface Props {
  affordance: Affordance;
  onApprove: (body: ApproveBody) => void;
  onFeedback: (message: string) => void;
  busy?: boolean;
}

/**
 * Renders a single pending affordance and the controls a human uses to resolve it.
 *
 * The rendered UI branches on the affordance type into either a choice-style flow
 * (radio options + Approve/Reject/Revise) or a question-style flow (text answer).
 * Decisions are emitted via the `onApprove` / `onFeedback` callbacks; this component
 * holds only transient form state and triggers no side effects on its own.
 *
 * @param props - See {@link Props}.
 * @returns The affordance card element.
 */
export default function AffordanceView({ affordance, onApprove, onFeedback, busy }: Props) {
  // Local form state. `chosen` seeds to the first option so Approve is valid immediately.
  // `edits` backs the revision textarea; `answer` backs the question/form textarea.
  const [chosen, setChosen] = useState<string>(affordance.options[0]?.id ?? "");
  const [edits, setEdits] = useState("");
  const [answer, setAnswer] = useState("");

  // Branch selectors: choice/confirm share the option-list UI; question/form share
  // the free-text answer UI. Kept as derived booleans so the JSX below stays flat.
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

      {/* Choice/confirm flow: pick one option, then approve / reject / request a revision. */}
      {isChoice && (
        <div className="space-y-3">
          {/* Radio list of selectable options. */}
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

          {/* Primary decisions: approve (with the selected option) or outright reject. */}
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

          {/* Revision path: instead of approving/rejecting, send edit instructions
              back to the planner. Disabled until non-whitespace text is entered. */}
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

      {/* Question/form flow: free-text answer sent via onFeedback.
          Submit is disabled until non-whitespace text is entered. */}
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
