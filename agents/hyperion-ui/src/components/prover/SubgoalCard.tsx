/**
 * SubgoalCard — the Run view centerpiece for ONE sub-goal. Walks the prover
 * pipeline in order and shows what each stage produced:
 *
 *   retrieve (Path A) ‖ synthesize (Path B) → verify → compare → abstract → discharged
 *
 * Lean source/types render via <LeanCode>/<LeanInline>; verdicts, scores and
 * mode (research/deploy) render as compact key/value chips.
 */
import type { ReactNode } from "react";

import LeanCode from "../LeanCode";
import {
  finalWinnerPath,
  type LeanCandidate,
  type ProofPath,
  type Subgoal,
} from "../../api/prover";

/** Inline Lean fragment (types, statements) in a math-glyph monospace face. */
function LeanInline({ children }: { children: string }) {
  return <code className="lean-inline">{children}</code>;
}

function Pill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "bad" | "a" | "b" | "muted";
}) {
  return <span className={`pill pill--${tone}`}>{children}</span>;
}

function PathPill({ path }: { path: ProofPath | null }) {
  if (path === "A") return <Pill tone="a">Path A · retrieve</Pill>;
  if (path === "B") return <Pill tone="b">Path B · synthesize</Pill>;
  return <Pill tone="muted">no winner</Pill>;
}

function VerifiedPill({ ok }: { ok: boolean }) {
  return ok ? <Pill tone="good">verified ✓</Pill> : <Pill tone="bad">unverified</Pill>;
}

/** A labeled pipeline stage block. */
function Stage({
  step,
  title,
  badge,
  children,
}: {
  step: number;
  title: string;
  badge?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="stage">
      <div className="stage__head">
        <span className="stage__step">{step}</span>
        <span className="stage__title">{title}</span>
        <span className="stage__badge">{badge}</span>
      </div>
      <div className="stage__body">{children}</div>
    </div>
  );
}

function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="kv">
      <span className="kv__k">{k}</span>
      <span className="kv__v">{v}</span>
    </div>
  );
}

function CandidateBlock({
  cand,
  label,
}: {
  cand: LeanCandidate;
  label?: string;
}) {
  return (
    <div className="cand">
      <div className="cand__meta">
        <KV k="statement" v={<LeanInline>{cand.statement}</LeanInline>} />
        <KV k="type" v={<LeanInline>{cand.lean_type}</LeanInline>} />
        <KV k="origin" v={<Pill tone="muted">{cand.origin}</Pill>} />
        {typeof cand.generality_score === "number" && (
          <KV k="generality" v={<span className="tabular-nums">{cand.generality_score}</span>} />
        )}
      </div>
      <LeanCode code={cand.source} label={label} />
    </div>
  );
}

export default function SubgoalCard({ id, sg }: { id: string; sg: Subgoal }) {
  const winner = finalWinnerPath(sg);
  const abstractFired = sg.abstracted != null;
  const vd = sg.verify_decision;
  const tl = sg.triple_log;

  return (
    <section className="card subgoal">
      <header className="subgoal__head">
        <div className="flex items-center gap-2">
          <span className="subgoal__id">{id}</span>
          <LeanInline>{sg.lean_type}</LeanInline>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <PathPill path={winner} />
          {abstractFired ? (
            <Pill tone="good">abstracted ✦</Pill>
          ) : (
            <Pill tone="muted">no abstraction</Pill>
          )}
          {sg.discharged ? (
            <Pill tone="good">discharged</Pill>
          ) : (
            <Pill tone="bad">open</Pill>
          )}
        </div>
      </header>

      <div className="subgoal__stages">
        {/* 1 — retrieve (Path A) */}
        <Stage
          step={1}
          title="Retrieve · Path A"
          badge={<VerifiedPill ok={sg.verified_a != null} />}
        >
          {sg.candidate_a ? (
            <>
              <CandidateBlock cand={sg.candidate_a} label="banked lemma" />
              {sg.candidates_a.length > 1 && (
                <div className="stage__note">
                  {sg.candidates_a.length} applicable lemmas retrieved
                </div>
              )}
            </>
          ) : (
            <div className="stage__empty">no lemma retrieved</div>
          )}
        </Stage>

        {/* 2 — synthesize (Path B) */}
        <Stage
          step={2}
          title="Synthesize · Path B"
          badge={<VerifiedPill ok={sg.verified_b != null} />}
        >
          {sg.candidate_b ? (
            <CandidateBlock cand={sg.candidate_b} label="fresh proof" />
          ) : (
            <div className="stage__empty">no candidate synthesized</div>
          )}
        </Stage>

        {/* 3 — verify (kernel verdicts + bounded repair) */}
        <Stage
          step={3}
          title="Verify"
          badge={vd ? <PathPill path={vd.winner_path} /> : undefined}
        >
          {vd ? (
            <div className="kv-grid">
              <KV
                k="verdicts"
                v={
                  <span className="flex flex-wrap gap-1">
                    {vd.verdicts.map((x, i) => (
                      <Pill key={i} tone={x.ok ? "good" : "bad"}>
                        {x.path}: {x.ok ? "ok" : "fail"}
                      </Pill>
                    ))}
                  </span>
                }
              />
              <KV k="A attempts" v={<span className="tabular-nums">{vd.a_attempts}</span>} />
              <KV k="repair iters" v={<span className="tabular-nums">{vd.repair_iters}</span>} />
              <KV k="mode" v={<Pill tone="muted">{vd.mode}</Pill>} />
            </div>
          ) : (
            <div className="stage__empty">no verify decision</div>
          )}
        </Stage>

        {/* 4 — compare (triple log) */}
        <Stage
          step={4}
          title="Compare"
          badge={
            tl ? (
              tl.compared ? (
                <Pill tone="good">A-vs-B contest</Pill>
              ) : (
                <Pill tone="muted">uncontested</Pill>
              )
            ) : undefined
          }
        >
          {tl ? (
            <div className="kv-grid">
              <KV k="winner" v={<PathPill path={tl.winner_path} />} />
              <KV
                k="generality (A / B → winner)"
                v={
                  <span className="tabular-nums">
                    {tl.scores.a} / {tl.scores.b} → {tl.scores.winner}
                  </span>
                }
              />
              <KV k="retrieved verified" v={<VerifiedPill ok={tl.retrieved_verified} />} />
              <KV k="synthesized verified" v={<VerifiedPill ok={tl.synthesized_verified} />} />
              <KV k="goal type" v={<LeanInline>{tl.goal_type}</LeanInline>} />
            </div>
          ) : (
            <div className="stage__empty">no triple logged</div>
          )}
        </Stage>

        {/* 5 — abstract (generalize Path-B lemma + re-verify) */}
        <Stage
          step={5}
          title="Abstract"
          badge={
            abstractFired ? <Pill tone="good">fired ✦</Pill> : <Pill tone="muted">skipped</Pill>
          }
        >
          {sg.abstracted ? (
            <>
              <div className="stage__note">
                Generalized statement:{" "}
                <LeanInline>{sg.abstracted.statement}</LeanInline>
              </div>
              <CandidateBlock cand={sg.abstracted} label="generalized lemma" />
            </>
          ) : (
            <div className="stage__empty">
              abstraction did not fire for this sub-goal
            </div>
          )}
        </Stage>

        {/* 6 — discharged (the proof that closed the sub-goal) */}
        <Stage
          step={6}
          title="Discharged"
          badge={sg.discharged ? <PathPill path={sg.discharged.path ?? null} /> : undefined}
        >
          {sg.discharged ? (
            <CandidateBlock cand={sg.discharged} label="discharged proof" />
          ) : (
            <div className="stage__empty">sub-goal left open</div>
          )}
        </Stage>
      </div>
    </section>
  );
}
