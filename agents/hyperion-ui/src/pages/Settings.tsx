/**
 * Settings.tsx — Hyperion UI "Settings" page.
 *
 * Role in the system:
 *   Rendered as a route in the Hyperion web console (Vite/React app on :4102, see
 *   `agents/hyperion-ui`). This page is the operator-facing control panel for global
 *   Hyperion configuration, talking to the Hyperion FastAPI backend (:4100) via the
 *   typed hooks in `../api/client`. All data flows through TanStack Query, so reads are
 *   cached and writes invalidate the relevant query keys to keep the UI in sync.
 *
 * What it lets the operator do:
 *   1. Role models   — map each logical agent role (planner / worker / cheap) to a
 *      concrete model alias served by the LiteLLM proxy (:4000). Edited in a local
 *      `draft` and committed via `useUpdateConfig`.
 *   2. Default workflow — choose which workflow DAG runs when a request doesn't pick
 *      one. The actual DAGs are authored on the separate "Workflows" tab.
 *   3. Caps          — read-only display of token/cost caps (configured via env or
 *      per-request; per-agent overrides live in the agent editor, not here).
 *   4. Agent store   — export the full agent config as a zip, or import one (server
 *      validates the whole set before writing).
 *   5. Providers     — read-only badges showing whether each provider has an API key.
 *
 * Key design decisions / non-obvious context:
 *   - `draft` holds the in-progress model selections so typing doesn't fire a save on
 *     every keystroke; `useUpdateConfig().mutate(draft)` commits the batch.
 *   - Model autocomplete (`<datalist>`) is de-duplicated because the LiteLLM proxy
 *     reports alias groups as models too (see comment at `choices`).
 *   - The bottom config cards (default workflow / caps / agent store / providers) only
 *     render once `config` has loaded, hence the `{config && (...)}` guard.
 *
 * @module pages/Settings
 */
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  exportConfigUrl,
  importConfig,
  useConfig,
  useModels,
  useUpdateConfig,
  useWorkflows,
} from "../api/client";
import { useToast } from "../components/Toast";

/**
 * Static descriptor for the three editable "role -> model" rows.
 *
 * - `key`   maps to the config field name sent to the backend (and the `draft` key).
 * - `label` is the human-readable role name shown in the row.
 * - `note`  is a short hint describing what that role is used for.
 *
 * Declared `as const` so `key` is narrowed to a string-literal union rather than
 * widened to `string`, keeping the `draft` indexing type-safe.
 */
const ROLES = [
  { key: "model_planner", label: "Planner", note: "high-stakes planning" },
  { key: "model_worker", label: "Worker", note: "research + synthesis" },
  { key: "model_cheap", label: "Cheap", note: "summarization sub-calls" },
] as const;

/**
 * Settings page component (default route export).
 *
 * Composes the global-configuration UI for Hyperion: role-model mapping, default
 * workflow selection, read-only caps, agent-store import/export, and provider key
 * status. Takes no props — it sources everything from the API hooks
 * (`useConfig`, `useModels`, `useWorkflows`, `useUpdateConfig`).
 *
 * Side effects:
 *   - Issues backend mutations via `useUpdateConfig().mutate(...)` (save models,
 *     change default workflow) and `importConfig(...)` (zip upload).
 *   - Invalidates the "agents", "groups", and "workflows" query caches after a
 *     successful import so dependent views refetch.
 *   - Surfaces toast notifications for import/workflow success and errors.
 *
 * @returns The Settings page JSX.
 */
export default function Settings() {
  const { data: config } = useConfig();
  const { data: models } = useModels();
  const { data: workflows } = useWorkflows();
  const update = useUpdateConfig();
  const toast = useToast();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [importing, setImporting] = useState(false);

  /**
   * Upload an agent-config zip to the backend and reconcile the UI on success.
   *
   * Flow: flips the `importing` flag (drives button disabled/label), POSTs the file
   * via `importConfig`, then toasts a summary (agents imported, plus optional
   * workflow count) and invalidates the agents/groups/workflows query caches so the
   * rest of the app refetches. Errors are caught and shown as an error toast.
   * The `finally` block always clears `importing` and resets the file input value so
   * selecting the same file again re-triggers `onChange`.
   *
   * @param file - The `.zip` config archive chosen by the operator.
   */
  async function onImportFile(file: File) {
    setImporting(true);
    try {
      const res = await importConfig(file);
      const wf = res.workflows?.length ? ` + ${res.workflows.length} workflow(s)` : "";
      toast.push(`Imported ${res.count} agent(s)${wf}: ${res.imported.join(", ")}`, "success");
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    } catch (e) {
      toast.push((e as Error).message, "error");
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  // Local, uncommitted edits for the role-model inputs, keyed by ROLES[].key.
  // Seeded from the server's current mapping by the effect below and only pushed
  // to the backend when "Save models" is clicked.
  const [draft, setDraft] = useState<Record<string, string>>({});

  // Hydrate `draft` from the server's current model mapping once `models.current`
  // is available (and re-sync if it changes), so the inputs reflect saved state.
  useEffect(() => {
    if (models?.current) {
      setDraft({
        model_planner: models.current.planner,
        model_worker: models.current.worker,
        model_cheap: models.current.cheap,
      });
    }
  }, [models?.current]);

  // Dedupe: the proxy reports the alias groups (smart/worker/cheap) as models too,
  // so a naive concat shows each twice in the autocomplete.
  const choices = Array.from(new Set([...(models?.aliases ?? []), ...(models?.models ?? [])]));

  return (
    <div className="mx-auto max-w-2xl">
      <h2 className="mb-4 text-lg font-semibold">Settings</h2>

      <div className="card mb-4 space-y-3">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Role models
        </h3>
        {ROLES.map((r) => {
          const val = draft[r.key] ?? "";
          // Show the fallback chain when the current value is a known alias.
          const chain = config?.alias_fallback_order?.[val];
          return (
            <div key={r.key} className="grid grid-cols-[1fr_2fr] items-start gap-3">
              <div>
                <div className="text-sm font-medium">{r.label}</div>
                <div className="text-xs text-slate-500">{r.note}</div>
              </div>
              <div>
                <input
                  className="input"
                  list="model-choices"
                  value={val}
                  onChange={(e) => setDraft((d) => ({ ...d, [r.key]: e.target.value }))}
                />
                {chain && (
                  <div className="mt-1 text-xs text-slate-500">
                    {val} → {chain.join(" → ")}
                  </div>
                )}
              </div>
            </div>
          );
        })}
        <datalist id="model-choices">
          {choices.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
        <div className="flex items-center gap-3">
          <button
            className="btn btn-primary"
            onClick={() => update.mutate(draft)}
            disabled={update.isPending}
          >
            {update.isPending ? "Saving…" : "Save models"}
          </button>
          {update.isError && (
            <span className="text-sm text-rose-300">{(update.error as Error).message}</span>
          )}
          {update.isSuccess && <span className="text-sm text-emerald-300">Saved</span>}
        </div>
      </div>

      {config && (
        <>
          <div className="card mb-4">
            <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
              Default workflow
            </h3>
            <p className="mb-3 text-xs text-slate-500">
              The workflow (DAG) used when a run doesn't pick one. Manage workflows on the
              Workflows tab.
            </p>
            <select
              className="input max-w-sm"
              value={config.default_workflow}
              disabled={update.isPending}
              onChange={(e) =>
                update.mutate(
                  { default_workflow: e.target.value },
                  {
                    onSuccess: () => toast.push(`Default workflow → ${e.target.value}`, "success"),
                    onError: (err) => toast.push((err as Error).message, "error"),
                  },
                )
              }
            >
              {(workflows ?? []).map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name || w.id}
                </option>
              ))}
            </select>
          </div>

          <div className="card mb-4">
            <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
              Caps
            </h3>
            <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
              {Object.entries(config.caps).map(([k, v]) => (
                <div key={k}>
                  <div className="text-xs text-slate-500">{k}</div>
                  <div className="font-mono">{v.toLocaleString()}</div>
                </div>
              ))}
            </div>
            <p className="mt-2 text-xs text-slate-500">
              Caps are set via env / per-request; per-agent overrides live in the agent editor.
            </p>
          </div>

          <div className="card mb-4">
            <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
              Agent store
            </h3>
            <p className="mb-3 text-xs text-slate-500">
              Export every agent record as a zip to back up or move between machines. Import
              validates the whole set (DAG + at-least-one plan/synthesize) before writing.
            </p>
            <div className="flex items-center gap-3">
              <a className="btn" href={exportConfigUrl()}>
                Export config
              </a>
              <button
                className="btn"
                disabled={importing}
                onClick={() => fileRef.current?.click()}
              >
                {importing ? "Importing…" : "Import config"}
              </button>
              <input
                ref={fileRef}
                type="file"
                accept=".zip"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) onImportFile(f);
                }}
              />
            </div>
          </div>

          <div className="card">
            <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
              Providers
            </h3>
            <div className="flex flex-wrap gap-2">
              {Object.entries(config.providers).map(([name, p]) => (
                <span
                  key={name}
                  className={`pill ${p.key_present ? "border-emerald-500/40 text-emerald-300" : "border-edge text-slate-500"}`}
                >
                  {name}: {p.key_present ? "ok" : "no key"}
                </span>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
