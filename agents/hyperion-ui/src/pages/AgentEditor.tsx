import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  useAgent,
  useAgents,
  useDeleteAgent,
  useDuplicateAgent,
  useModels,
  useSaveAgent,
  useTools,
  type AgentRecord,
  type Stage,
  type TriggerType,
} from "../api/client";
import InfoTip from "../components/InfoTip";

const STAGES: Stage[] = ["plan", "work", "synthesize"];
const TRIGGER_TYPES: TriggerType[] = ["always", "keyword", "task_type", "upstream", "schedule"];

function blankAgent(): AgentRecord {
  return {
    id: "",
    name: "",
    description: "",
    group: "optional",
    active: true,
    stage: "work",
    role: "",
    goal: "",
    backstory: "",
    model_alias: "worker",
    fallback_alias: null,
    temperature: 0.2,
    top_p: null,
    max_tokens: null,
    max_iter: 5,
    tools: [],
    trigger: { type: "always", keywords: [], task_types: [], upstream: [], cron: null },
    order: 1,
    thresholds: {
      max_input_tokens: null,
      max_output_tokens: null,
      max_activations_per_day: null,
    },
  };
}

function csv(v: string): string[] {
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}

export default function AgentEditor() {
  const { id } = useParams();
  const isNew = !id;
  const nav = useNavigate();

  const existing = useAgent(id);
  const { data: tools } = useTools();
  const { data: models } = useModels();
  const { data: allAgents } = useAgents();

  const save = useSaveAgent(isNew);
  const del = useDeleteAgent();
  const dup = useDuplicateAgent();

  const [rec, setRec] = useState<AgentRecord>(blankAgent());

  useEffect(() => {
    if (existing.data) setRec(existing.data);
  }, [existing.data]);

  const dependents = useMemo(
    () => (allAgents ?? []).filter((a) => a.id !== rec.id && a.trigger.upstream.includes(rec.id)),
    [allAgents, rec.id],
  );

  function set<K extends keyof AgentRecord>(k: K, v: AgentRecord[K]) {
    setRec((r) => ({ ...r, [k]: v }));
  }
  function setTrigger<K extends keyof AgentRecord["trigger"]>(k: K, v: AgentRecord["trigger"][K]) {
    setRec((r) => ({ ...r, trigger: { ...r.trigger, [k]: v } }));
  }
  function setThreshold(k: keyof AgentRecord["thresholds"], v: string) {
    const n = v === "" ? null : Number(v);
    setRec((r) => ({ ...r, thresholds: { ...r.thresholds, [k]: n } }));
  }
  function toggleTool(name: string) {
    setRec((r) => ({
      ...r,
      tools: r.tools.includes(name) ? r.tools.filter((t) => t !== name) : [...r.tools, name],
    }));
  }

  function onSave() {
    save.mutate(rec, { onSuccess: () => nav("/") });
  }
  function onDelete() {
    if (!id) return;
    if (!confirm(`Delete agent "${rec.name}"?`)) return;
    del.mutate(id, { onSuccess: () => nav("/") });
  }
  function onDuplicate() {
    if (!id) return;
    dup.mutate(id, { onSuccess: (clone) => nav(`/agents/${clone.id}`) });
  }

  if (!isNew && existing.isLoading) return <p className="text-slate-400">Loading…</p>;

  const aliasList = models?.aliases ?? ["smart", "worker", "cheap", "fast"];
  // Aliases first (recommended), then concrete model ids — deduped, since the
  // proxy reports the alias groups as models too.
  const modelChoices = Array.from(new Set([...aliasList, ...(models?.models ?? [])]));
  const editingCore = rec.group === "core";

  return (
    <div className="mx-auto max-w-3xl">
      <h2 className="mb-4 text-lg font-semibold">{isNew ? "New agent" : `Edit: ${rec.name}`}</h2>

      {(editingCore || dependents.length > 0) && !isNew && (
        <div className="card mb-4 border-amber-500/40 bg-amber-500/10 text-sm text-amber-200">
          {editingCore && <p>This is a <b>core</b> agent — its prompt shapes the default pipeline. Edit carefully.</p>}
          {dependents.length > 0 && (
            <p className="mt-1">
              Depended on by: {dependents.map((d) => d.id).join(", ")}. Changing its stage/id may
              break their routing.
            </p>
          )}
        </div>
      )}

      <div className="space-y-4">
        {/* Identity */}
        <div className="card grid grid-cols-2 gap-3">
          <div>
            <label className="label">
              ID (slug)
              <InfoTip text="Unique identifier used in routing and config files. Lowercase letters, numbers, hyphens and underscores only. Fixed once created." />
            </label>
            <input
              className="input"
              value={rec.id}
              disabled={!isNew}
              onChange={(e) => set("id", e.target.value)}
            />
          </div>
          <div>
            <label className="label">
              Name
              <InfoTip text="Human-readable display name shown on the dashboard and in monitoring." />
            </label>
            <input className="input" value={rec.name} onChange={(e) => set("name", e.target.value)} />
          </div>
          <div className="col-span-2">
            <label className="label">
              Description
              <InfoTip text="Short summary of what this agent does. Shown on its dashboard card." />
            </label>
            <input
              className="input"
              value={rec.description}
              onChange={(e) => set("description", e.target.value)}
            />
          </div>
          <div>
            <label className="label">
              Group
              <InfoTip text="Organizational label used to group and filter agents on the dashboard (e.g. core, optional). Agents in the 'core' group form the default pipeline." />
            </label>
            <input className="input" value={rec.group} onChange={(e) => set("group", e.target.value)} />
          </div>
          <div>
            <label className="label">
              Stage
              <InfoTip text="Where this agent runs in the pipeline: plan (decompose the task into a plan), work (research/execute the steps), or synthesize (write the final report)." />
            </label>
            <select className="input" value={rec.stage} onChange={(e) => set("stage", e.target.value as Stage)}>
              {STAGES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <label className="col-span-2 flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={rec.active}
              onChange={(e) => set("active", e.target.checked)}
            />
            Active
          </label>
        </div>

        {/* Prompt */}
        <div className="card space-y-3">
          <div>
            <label className="label">
              Role
              <InfoTip text="The agent's job title / persona, e.g. 'Senior Research Analyst'. Sets the perspective the model adopts when reasoning." />
            </label>
            <input className="input" value={rec.role} onChange={(e) => set("role", e.target.value)} />
          </div>
          <div>
            <label className="label">
              Goal
              <InfoTip text="The objective this agent pursues each run — its core instruction. Be specific; this is the primary driver of the model's behavior." />
            </label>
            <textarea
              className="input min-h-[120px] resize-y"
              value={rec.goal}
              onChange={(e) => set("goal", e.target.value)}
            />
          </div>
          <div>
            <label className="label">
              Backstory
              <InfoTip text="Background and personality that shape how the agent approaches its goal — expertise, tone, and any standing constraints." />
            </label>
            <textarea
              className="input min-h-[80px] resize-y"
              value={rec.backstory}
              onChange={(e) => set("backstory", e.target.value)}
            />
          </div>
        </div>

        {/* Model */}
        <div className="card grid grid-cols-2 gap-3">
          <div>
            <label className="label">
              Model
              <InfoTip text="Which model powers this agent. Pick a role alias (smart/worker/cheap/fast) to inherit the global routing with automatic provider fallback, or choose a concrete model id (e.g. gemini-2.5-pro) to pin one exactly." />
            </label>
            <input
              className="input"
              list="model-choices"
              value={rec.model_alias}
              onChange={(e) => set("model_alias", e.target.value)}
            />
            <datalist id="model-choices">
              {modelChoices.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </div>
          <div>
            <label className="label">
              Fallback model
              <InfoTip text="Optional. If the primary model's call fails, the agent retries once against this model. Pick an alias or a concrete model id. Leave blank for no per-agent fallback (alias routing still applies its own proxy-level fallbacks)." />
            </label>
            <input
              className="input"
              list="model-choices"
              placeholder="(none)"
              value={rec.fallback_alias ?? ""}
              onChange={(e) => set("fallback_alias", e.target.value || null)}
            />
          </div>
          <div>
            <label className="label">
              Temperature
              <InfoTip text="Controls randomness of the model's output (0–2). Lower (0–0.3) is focused and deterministic — best for research and analysis; higher (0.7–1.0) is more creative and varied. Hyperion defaults to 0.1–0.2." />
            </label>
            <input
              className="input"
              type="number"
              step="0.1"
              value={rec.temperature}
              onChange={(e) => set("temperature", Number(e.target.value))}
            />
          </div>
          <div>
            <label className="label">
              Max iter
              <InfoTip text="Maximum reasoning/tool-use loops this agent may take in a single run (one LLM call per loop). Higher allows more thorough work but costs more tokens and time." />
            </label>
            <input
              className="input"
              type="number"
              value={rec.max_iter}
              onChange={(e) => set("max_iter", Number(e.target.value))}
            />
          </div>
        </div>

        {/* Tools */}
        <div className="card">
          <label className="label">
            Tools
            <InfoTip text="External capabilities this agent may call during a run — e.g. web search, second-brain retrieval, code execution. Hover each to see what it does." />
          </label>
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            {(tools ?? []).map((t) => (
              <label key={t.name} className="flex items-center gap-2 text-sm" title={t.description}>
                <input
                  type="checkbox"
                  checked={rec.tools.includes(t.name)}
                  onChange={() => toggleTool(t.name)}
                />
                {t.name}
              </label>
            ))}
          </div>
        </div>

        {/* Trigger */}
        <div className="card space-y-3">
          <div>
            <label className="label">
              Trigger type
              <InfoTip text="When this agent activates: always (every run), keyword (request contains a term), task_type (matches a classified type), upstream (after named agents finish), or schedule (on a cron timer)." />
            </label>
            <select
              className="input"
              value={rec.trigger.type}
              onChange={(e) => setTrigger("type", e.target.value as TriggerType)}
            >
              {TRIGGER_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          {rec.trigger.type === "keyword" && (
            <div>
              <label className="label">Keywords (comma-separated)</label>
              <input
                className="input"
                value={rec.trigger.keywords.join(", ")}
                onChange={(e) => setTrigger("keywords", csv(e.target.value))}
              />
            </div>
          )}
          {rec.trigger.type === "task_type" && (
            <div>
              <label className="label">Task types (comma-separated)</label>
              <input
                className="input"
                value={rec.trigger.task_types.join(", ")}
                onChange={(e) => setTrigger("task_types", csv(e.target.value))}
              />
            </div>
          )}
          {rec.trigger.type === "upstream" && (
            <div>
              <label className="label">Upstream agent ids (comma-separated)</label>
              <input
                className="input"
                value={rec.trigger.upstream.join(", ")}
                onChange={(e) => setTrigger("upstream", csv(e.target.value))}
              />
            </div>
          )}
          {rec.trigger.type === "schedule" && (
            <div>
              <label className="label">Cron</label>
              <input
                className="input"
                placeholder="*/5 * * * *"
                value={rec.trigger.cron ?? ""}
                onChange={(e) => setTrigger("cron", e.target.value || null)}
              />
            </div>
          )}
        </div>

        {/* Thresholds */}
        <div className="card grid grid-cols-3 gap-3">
          <div>
            <label className="label">
              Max input tokens
              <InfoTip text="Per-run cap on tokens sent to the model by this agent. The run aborts (circuit breaker) if exceeded. Leave blank to inherit the global cap / no limit." />
            </label>
            <input
              className="input"
              type="number"
              value={rec.thresholds.max_input_tokens ?? ""}
              onChange={(e) => setThreshold("max_input_tokens", e.target.value)}
            />
          </div>
          <div>
            <label className="label">
              Max output tokens
              <InfoTip text="Per-run cap on tokens this agent may generate. The run aborts if exceeded. Leave blank to inherit the global cap / no limit." />
            </label>
            <input
              className="input"
              type="number"
              value={rec.thresholds.max_output_tokens ?? ""}
              onChange={(e) => setThreshold("max_output_tokens", e.target.value)}
            />
          </div>
          <div>
            <label className="label">
              Max activations/day
              <InfoTip text="Cap on how many times this agent may run in a 24-hour window — useful for scheduled/triggered agents. Leave blank for unlimited." />
            </label>
            <input
              className="input"
              type="number"
              value={rec.thresholds.max_activations_per_day ?? ""}
              onChange={(e) => setThreshold("max_activations_per_day", e.target.value)}
            />
          </div>
        </div>

        {save.isError && (
          <div className="card border-rose-500/40 bg-rose-500/10 text-sm text-rose-200">
            {(save.error as Error).message}
          </div>
        )}

        <div className="flex items-center gap-2">
          <button className="btn btn-primary" onClick={onSave} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button className="btn" onClick={() => nav("/")}>
            Cancel
          </button>
          {!isNew && (
            <>
              <button className="btn" onClick={onDuplicate} disabled={dup.isPending}>
                Duplicate
              </button>
              <button className="btn btn-danger ml-auto" onClick={onDelete} disabled={del.isPending}>
                Delete
              </button>
            </>
          )}
        </div>
        {del.isError && <p className="text-sm text-rose-300">{(del.error as Error).message}</p>}
      </div>
    </div>
  );
}
