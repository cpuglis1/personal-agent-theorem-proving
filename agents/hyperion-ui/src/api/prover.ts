/**
 * prover.ts — Read-only data layer for the Hyperion *prover* trace views.
 *
 * The console's main API client (./client.ts) already speaks to the Hyperion
 * backend (:4100); this module is the prover-specific slice. It is intentionally
 * thin and additive so the existing console is untouched:
 *
 *   - Types mirror the per-stage prover trace returned under `prover` by
 *     GET /tasks/{id}/trace (null for non-prover tasks). See the backend prover
 *     workflow: decompose -> retrieve ‖ synthesize -> verify -> bank, with
 *     definition-synthesis escalation through prove_through on stalls.
 *   - `useProverTrace(source, taskId)` is the single hook the pages consume. It
 *     supports two data sources behind one toggle:
 *       • "fixture" — bundled fixtures/sample-trace.json, so the Run view renders
 *         with no backend (offline-first dev / demo).
 *       • "live"    — fetch the real trace from :4100 for a pasted task_id.
 *
 * We reuse {@link API_BASE} from ./client so the backend URL (and its
 * VITE_HYPERION_API override) stays defined in exactly one place.
 */
import { useQuery } from "@tanstack/react-query";

import { API_BASE } from "./client";

/** Where a candidate proof came from in the prover pipeline. */
export type CandidateOrigin =
  | "retrieve"
  | "synthesize"
  | "repair"
  | "battery"
  | "concept";

/** Which path closed a subgoal: A = retrieve/cache, B = synth/repair, C = concept. */
export type ProofPath = "A" | "B" | "C";

/** A single Lean proof candidate produced by one stage of the pipeline. */
export interface LeanCandidate {
  source: string;
  statement: string;
  proof_term: string;
  origin: CandidateOrigin | string;
  lean_type: string;
  path?: ProofPath | null;
  generality_score?: number | null;
}

export interface ProveThroughTrace {
  subgoal?: string;
  ran?: boolean;
  concept_id?: string | null;
  solved?: boolean;
  axioms_clean?: boolean | null;
  repair_iters?: number;
  reason?: string;
}

/** One kernel verdict in the verify race. */
export interface VerifyVerdict {
  path: string;
  ok: boolean;
}

/** The verify stage's decision: which path won + repair-loop accounting. */
export interface VerifyDecision {
  subgoal?: string;
  winner_path: ProofPath | null;
  a_attempts: number;
  repair_iters: number;
  mode?: string;
  verdicts: VerifyVerdict[];
}

/** Full per-sub-goal trace across the pipeline stages. */
export interface Subgoal {
  lean_type: string;
  candidate_a: LeanCandidate | null;
  candidate_b: LeanCandidate | null;
  candidates_a: LeanCandidate[];
  verified_a: LeanCandidate | null;
  verified_b: LeanCandidate | null;
  verify_decision: VerifyDecision | null;
  escalated?: boolean | null;
  concept_candidates?: unknown[];
  verify_concept?: unknown | null;
  verified_concept?: unknown | null;
  prove_through?: ProveThroughTrace | null;
  accepted_concept?: unknown | null;
  bank_concept?: unknown | null;
  discharged: LeanCandidate | null;
}

/** The `prover` object: per-stage trace for a whole proof run. */
export interface ProverTrace {
  request: string;
  status: string;
  scaffold: string | null;
  skeleton_ok: boolean | null;
  result_lean: string | null;
  subgoals: Record<string, Subgoal>;
}

/**
 * Normalized trace envelope the Run view consumes. For "live" this is the real
 * GET /tasks/{id}/trace body; for "fixture" we wrap the bundled prover payload
 * (which is the `prover` object itself) into the same shape.
 */
export interface ProverTraceResponse {
  task_id: string;
  request: string;
  status: string;
  prover: ProverTrace | null;
}

export type TraceSource = "fixture" | "live";

/**
 * Load the bundled sample trace. The fixture file is the raw `prover` payload
 * (real backend output), so we wrap it into a {@link ProverTraceResponse}.
 */
export async function loadFixtureTrace(): Promise<ProverTraceResponse> {
  const mod = await import("../../fixtures/sample-trace.json");
  const prover = (mod.default ?? mod) as unknown as ProverTrace;
  return {
    task_id: "fixture · sample-trace.json",
    request: prover.request,
    status: prover.status,
    prover,
  };
}

/**
 * Fetch a real prover trace from the backend (:4100) for `taskId`. Throws a
 * readable Error on a non-2xx response or unreachable backend, so the Run view
 * can show a graceful message when the Docker stack is down.
 */
export async function fetchLiveTrace(taskId: string): Promise<ProverTraceResponse> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}/tasks/${taskId}/trace`, {
      headers: { "content-type": "application/json" },
    });
  } catch (e) {
    throw new Error(
      `Could not reach the Hyperion backend at ${API_BASE} — is the Docker stack up? (${
        e instanceof Error ? e.message : String(e)
      })`,
    );
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json() as Promise<ProverTraceResponse>;
}

/**
 * Query hook backing the Run view. Switches data source via `source`; the "live"
 * query stays idle until a `taskId` is provided.
 */
export function useProverTrace(source: TraceSource, taskId?: string) {
  return useQuery<ProverTraceResponse>({
    queryKey: ["prover-trace", source, taskId ?? null],
    queryFn: () =>
      source === "fixture" ? loadFixtureTrace() : fetchLiveTrace(taskId as string),
    enabled: source === "fixture" || !!taskId,
    retry: source === "live" ? 1 : 0,
  });
}

// ---------------------------------------------------------------------------
// Derived "thesis" metrics — computed client-side from the sub-goal map.
// ---------------------------------------------------------------------------

/**
 * The final winning path for a sub-goal: the discharged proof's path, falling
 * back to the verify winner.
 */
export function finalWinnerPath(sg: Subgoal): ProofPath | null {
  return sg.discharged?.path ?? sg.verify_decision?.winner_path ?? null;
}

export interface ThesisStats {
  total: number;
  solved: number;
  solvedRate: number;
  pathAWins: number;
  pathBWins: number;
  pathCWins: number;
  /** Path-A (retrieval) win-rate among *solved* sub-goals. */
  pathAWinRate: number;
  escalations: number;
  conceptsVerified: number;
  proveThroughSolved: number;
}

/** Aggregate the thesis read-out across all sub-goals of a run. */
export function thesisStats(subgoals: Record<string, Subgoal>): ThesisStats {
  const goals = Object.values(subgoals);
  const total = goals.length;
  const solvedGoals = goals.filter((g) => g.discharged != null);
  const solved = solvedGoals.length;
  let pathAWins = 0;
  let pathBWins = 0;
  let pathCWins = 0;
  for (const g of solvedGoals) {
    const w = finalWinnerPath(g);
    if (w === "A") pathAWins += 1;
    else if (w === "B") pathBWins += 1;
    else if (w === "C") pathCWins += 1;
  }
  return {
    total,
    solved,
    solvedRate: total ? solved / total : 0,
    pathAWins,
    pathBWins,
    pathCWins,
    pathAWinRate: solved ? pathAWins / solved : 0,
    escalations: goals.filter((g) => g.escalated === true).length,
    conceptsVerified: goals.filter((g) => g.verified_concept != null).length,
    proveThroughSolved: goals.filter((g) => g.prove_through?.solved === true).length,
  };
}
