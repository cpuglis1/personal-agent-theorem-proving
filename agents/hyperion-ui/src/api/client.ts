import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";

export const API_BASE =
  (import.meta.env.VITE_HYPERION_API as string | undefined)?.replace(/\/$/, "") ||
  "http://localhost:4100";

// ---------------------------------------------------------------------------
// Types — mirror the Hyperion backend records (registry.py / api.py).
// ---------------------------------------------------------------------------

export type Stage = "plan" | "work" | "synthesize";
export type TriggerType = "always" | "keyword" | "task_type" | "upstream" | "schedule";

export interface Trigger {
  type: TriggerType;
  keywords: string[];
  task_types: string[];
  upstream: string[];
  cron: string | null;
}

export interface Thresholds {
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  max_activations_per_day: number | null;
}

export interface AgentRecord {
  id: string;
  name: string;
  description: string;
  group: string;
  active: boolean;
  stage: Stage;
  role: string;
  goal: string;
  backstory: string;
  model_alias: string;
  fallback_alias: string | null;
  temperature: number;
  top_p: number | null;
  max_tokens: number | null;
  max_iter: number;
  tools: string[];
  trigger: Trigger;
  order: number;
  thresholds: Thresholds;
}

export interface ToolInfo {
  name: string;
  description: string;
}

export interface ModelsInfo {
  aliases: string[];
  models: string[];
  current: { planner: string; worker: string; cheap: string };
}

export interface AffordanceOption {
  id: string;
  label: string;
  description: string;
}

export interface AffordanceField {
  id: string;
  label: string;
  type: "text" | "number" | "boolean" | "select";
  options: string[];
  required: boolean;
}

export interface Affordance {
  type: "choice" | "form" | "question" | "confirm";
  prompt: string;
  options: AffordanceOption[];
  fields: AffordanceField[];
  agent_id: string | null;
  stage: string | null;
}

export interface RoutingResult {
  selected_agents: string[];
  skipped: { id: string; reason: string }[];
  dag: Record<string, string[]>;
}

export type TaskStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "awaiting_input"
  | "done"
  | "failed";

export interface TaskResponse {
  task_id: string;
  status: TaskStatus;
  error: string | null;
  result_path: string | null;
  progress_lines: string[];
  routing: RoutingResult | null;
  pending_stage: string | null;
  pending_affordance: Affordance | null;
}

export interface ConfigResponse {
  models: Record<string, { alias: string; note: string; env_var: string }>;
  providers: Record<string, { key_present: boolean; status: string }>;
  caps: Record<string, number>;
  default_workflow: string;
  litellm_url: string;
}

export interface WorkflowNode {
  id: string;
  agent: string;
  upstream: string[];
  gate_before: boolean;
  instruction: string | null;
}

export interface WorkflowRecord {
  id: string;
  name: string;
  description: string;
  nodes: WorkflowNode[];
}

// ---------------------------------------------------------------------------
// Fetch helper
// ---------------------------------------------------------------------------

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(API_BASE + path, {
    headers: { "content-type": "application/json" },
    ...init,
  });
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
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

export function useAgents() {
  return useQuery({ queryKey: ["agents"], queryFn: () => req<AgentRecord[]>("/agents") });
}

export function useAgent(id: string | undefined) {
  return useQuery({
    queryKey: ["agent", id],
    queryFn: () => req<AgentRecord>(`/agents/${id}`),
    enabled: !!id,
  });
}

export function useSaveAgent(isNew: boolean) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (rec: AgentRecord) =>
      req<AgentRecord>(isNew ? "/agents" : `/agents/${rec.id}`, {
        method: isNew ? "POST" : "PUT",
        body: JSON.stringify(rec),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });
}

export function useDeleteAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => req(`/agents/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });
}

export function useDuplicateAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      req<AgentRecord>(`/agents/${id}/duplicate`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });
}

export function useGroups() {
  return useQuery({ queryKey: ["groups"], queryFn: () => req<string[]>("/groups") });
}

export function useTools() {
  return useQuery({ queryKey: ["tools"], queryFn: () => req<ToolInfo[]>("/tools") });
}

export function useModels() {
  return useQuery({ queryKey: ["models"], queryFn: () => req<ModelsInfo>("/models") });
}

export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: () => req<ConfigResponse>("/config") });
}

export function useUpdateConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (
      body: Partial<
        Record<"model_planner" | "model_worker" | "model_cheap" | "default_workflow", string>
      >,
    ) => req<ConfigResponse>("/config", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["models"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Workflows — named DAGs of agent nodes (run picker + editor)
// ---------------------------------------------------------------------------

export function useWorkflows() {
  return useQuery({
    queryKey: ["workflows"],
    queryFn: () => req<WorkflowRecord[]>("/workflows"),
  });
}

export function useWorkflow(id: string | undefined) {
  return useQuery({
    queryKey: ["workflow", id],
    queryFn: () => req<WorkflowRecord>(`/workflows/${id}`),
    enabled: !!id,
  });
}

export function useSaveWorkflow(isNew: boolean) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (rec: WorkflowRecord) =>
      req<WorkflowRecord>(isNew ? "/workflows" : `/workflows/${rec.id}`, {
        method: isNew ? "POST" : "PUT",
        body: JSON.stringify(rec),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workflows"] }),
  });
}

export function useDeleteWorkflow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => req(`/workflows/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workflows"] }),
  });
}

export function useDuplicateWorkflow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      req<WorkflowRecord>(`/workflows/${id}/duplicate`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workflows"] }),
  });
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

export interface SubmitTaskBody {
  task: string;
  hitl?: "off" | "plan" | "full";
  workflow?: string | null;
}

export function useSubmitTask() {
  return useMutation({
    mutationFn: (body: SubmitTaskBody) =>
      req<TaskResponse>("/tasks", { method: "POST", body: JSON.stringify(body) }),
  });
}

export function useTask(
  id: string | undefined,
  opts?: Partial<UseQueryOptions<TaskResponse>>,
) {
  return useQuery({
    queryKey: ["task", id],
    queryFn: () => req<TaskResponse>(`/tasks/${id}`),
    enabled: !!id,
    ...opts,
  });
}

export interface ApproveBody {
  action: "approve" | "revise" | "reject";
  chosen_option?: string;
  edits?: string;
}

export function useApproveTask(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ApproveBody) =>
      req<TaskResponse>(`/tasks/${id}/approve`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["task", id] }),
  });
}

export function useFeedbackTask(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (message: string) =>
      req<TaskResponse>(`/tasks/${id}/feedback`, {
        method: "POST",
        body: JSON.stringify({ message }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["task", id] }),
  });
}

export interface NotionPageResult {
  url: string | null;
  id: string | null;
}

export function useSaveToNotion(id: string) {
  return useMutation({
    mutationFn: (title?: string) =>
      req<NotionPageResult>(`/tasks/${id}/save-to-notion`, {
        method: "POST",
        body: JSON.stringify({ title: title ?? null }),
      }),
  });
}

// ---------------------------------------------------------------------------
// Config export / import (Phase 9) — binary zip, handled outside the JSON helper
// ---------------------------------------------------------------------------

export function exportConfigUrl(): string {
  return `${API_BASE}/config/export`;
}

export async function importConfig(
  file: File,
): Promise<{ imported: string[]; count: number; workflows: string[] }> {
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch(`${API_BASE}/config/import`, { method: "POST", body: form });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------------
// Monitoring — run history, per-agent metrics, thresholds (Phase 8)
// ---------------------------------------------------------------------------

export interface TaskListItem {
  task_id: string;
  status: TaskStatus;
  request: string;
  error: string | null;
  created_at: string;
  updated_at: string;
  hitl: string | null;
  langfuse_url: string | null;
}

export interface TasksPage {
  total: number;
  limit: number;
  offset: number;
  items: TaskListItem[];
}

export function useTasks(limit = 50, offset = 0) {
  return useQuery({
    queryKey: ["tasks", limit, offset],
    queryFn: () => req<TasksPage>(`/tasks?limit=${limit}&offset=${offset}`),
    refetchInterval: 5000,
  });
}

export interface AgentMetric {
  id: string;
  name: string;
  stage: Stage;
  active: boolean;
  activations: number;
  errors: number;
  error_rate: number;
  tokens: { input: number; output: number };
  thresholds: Thresholds;
}

export interface MetricsResponse {
  tasks_total: number;
  by_status: Record<string, number>;
  caps: Record<string, number>;
  agents: AgentMetric[];
}

export function useMetrics() {
  return useQuery({
    queryKey: ["metrics"],
    queryFn: () => req<MetricsResponse>("/metrics"),
    refetchInterval: 5000,
  });
}

export interface ThresholdsResponse {
  global: Record<string, number>;
  agents: Record<string, Thresholds>;
}

export function useThresholds() {
  return useQuery({
    queryKey: ["thresholds"],
    queryFn: () => req<ThresholdsResponse>("/thresholds"),
  });
}

export interface ThresholdUpdateBody {
  cap_input_tokens?: number;
  cap_output_tokens?: number;
  cap_tool_loop?: number;
  cap_wall_seconds?: number;
  agents?: Record<string, Partial<Thresholds>>;
}

export function useUpdateThresholds() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ThresholdUpdateBody) =>
      req<ThresholdsResponse>("/thresholds", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["thresholds"] });
      qc.invalidateQueries({ queryKey: ["metrics"] });
    },
  });
}
