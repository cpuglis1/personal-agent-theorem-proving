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

const ROLES = [
  { key: "model_planner", label: "Planner", note: "high-stakes planning" },
  { key: "model_worker", label: "Worker", note: "research + synthesis" },
  { key: "model_cheap", label: "Cheap", note: "summarization sub-calls" },
] as const;

export default function Settings() {
  const { data: config } = useConfig();
  const { data: models } = useModels();
  const { data: workflows } = useWorkflows();
  const update = useUpdateConfig();
  const toast = useToast();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [importing, setImporting] = useState(false);

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

  const [draft, setDraft] = useState<Record<string, string>>({});

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
        {ROLES.map((r) => (
          <div key={r.key} className="grid grid-cols-[1fr_2fr] items-center gap-3">
            <div>
              <div className="text-sm font-medium">{r.label}</div>
              <div className="text-xs text-slate-500">{r.note}</div>
            </div>
            <input
              className="input"
              list="model-choices"
              value={draft[r.key] ?? ""}
              onChange={(e) => setDraft((d) => ({ ...d, [r.key]: e.target.value }))}
            />
          </div>
        ))}
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
