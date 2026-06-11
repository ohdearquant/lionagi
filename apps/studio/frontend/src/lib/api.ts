import type {
  AgentProfile,
  AgentProfileSummary,
  ArtifactContract,
  ArtifactVerification,
  DeclarativeArgSpec,
  DeclarativePlaybookData,
  PlaybookFormat,
  ProjectDetail,
  ProjectSummary,
  RunDetail,
  RunSummary,
  ScheduleDetail,
  ScheduleRunSummary,
  ScheduleSummary,
  ShowDetail,
  ShowEvent,
  ShowSummary,
  WorkerFormData,
  WorkerGraph,
  WorkerRaw,
  WorkerStepNode,
  WorkerLinkEdge,
  WorkerSummary,
} from "./types";

declare global {
  interface Window {
    __STUDIO_API_BASE__?: string;
    __STUDIO_AUTH_TOKEN__?: string;
  }
}

/** Return the per-launch bearer token injected by the desktop shell, if any. */
export function resolveAuthToken(): string | undefined {
  if (typeof window !== "undefined" && window.__STUDIO_AUTH_TOKEN__) {
    return window.__STUDIO_AUTH_TOKEN__;
  }
  return undefined;
}

export function resolveApiBase(): string {
  // Priority: window.__STUDIO_API_BASE__ (runtime injection) >
  // VITE_STUDIO_API_BASE (build-time env) > origin logic.
  // Treat empty string as "not configured" — defense against baking an empty
  // env var that silently produced same-origin /api/* requests.
  if (typeof window !== "undefined" && window.__STUDIO_API_BASE__) {
    return window.__STUDIO_API_BASE__;
  }
  const viteEnv = import.meta.env.VITE_STUDIO_API_BASE as string | undefined;
  if (viteEnv) return viteEnv;
  if (typeof window !== "undefined") {
    const port = window.location.port;
    // Vite dev-server ports: forward to the backend on the same hostname.
    if (port === "3000" || port === "5173") {
      return `${window.location.protocol}//${window.location.hostname}:8765`;
    }
    // Production / single-origin deployment: use same origin (relative URLs).
    return "";
  }
  // SSR / test environment without window: fall back to localhost for compat.
  return "http://localhost:8765";
}

export const API_BASE = resolveApiBase();

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;

  // Attach the desktop-shell bearer token when present.
  const token = resolveAuthToken();
  const headers: HeadersInit = { ...(init?.headers ?? {}) };
  if (token) {
    (headers as Record<string, string>)["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(url, { redirect: "follow", ...init, headers });
  if (!response.ok) {
    // Preserve the backend `detail` field (FastAPI/Pydantic validation errors,
    // our structured 409 body, etc.) so callers can surface it to the operator.
    // Falls back to the status code when the body is not JSON or has no detail.
    let detail: string | undefined;
    try {
      const body = (await response.json()) as { detail?: string };
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // not JSON — ignore
    }
    throw new Error(detail ?? `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

// Fetch-based server-sent-events subscription. Native EventSource cannot
// attach the Authorization header the desktop shell's per-launch bearer token
// requires; fetch + ReadableStream can. Mirrors EventSource semantics for the
// studio endpoints: unnamed `data: <json>\n\n` frames, auto-reconnect after
// 2s unless closed. Callers parse the JSON and call the returned closer on
// their terminal "done" frame.
function sseSubscribe(path: string, onData: (data: string) => void): () => void {
  const controller = new AbortController();
  let closed = false;
  const close = () => {
    closed = true;
    controller.abort();
  };

  void (async () => {
    while (!closed) {
      try {
        const token = resolveAuthToken();
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const response = await fetch(`${API_BASE}${path}`, {
          headers,
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          throw new Error(`SSE request failed: ${response.status}`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let sep: number;
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const data = frame
              .split("\n")
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).replace(/^ /, ""))
              .join("\n");
            if (data && !closed) onData(data);
          }
        }
      } catch {
        // Aborted by close(), or a network error worth retrying.
      }
      if (!closed) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    }
  })();

  return close;
}

// ─── Runs ─────────────────────────────────────────────────────────────────────

export interface RunListParams {
  page?: number;
  per_page?: number;
  status?: string[];
  playbook?: string;
  project?: string;
}

export interface RunListResponse {
  runs: RunSummary[];
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

export async function listRuns(params?: RunListParams): Promise<RunListResponse> {
  const query = new URLSearchParams();
  if (params?.page != null) query.set("page", String(params.page));
  if (params?.per_page != null) query.set("per_page", String(params.per_page));
  if (params?.playbook) query.set("playbook", params.playbook);
  if (params?.project) query.set("project", params.project);
  for (const value of params?.status ?? []) query.append("status", value);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return fetchJson<RunListResponse>(`/api/runs${suffix}`);
}

export async function getRun(runId: string): Promise<RunDetail> {
  return fetchJson<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`);
}

// ─── Workers (playbooks) ──────────────────────────────────────────────────────

interface PlaybookListEntry {
  name: string;
  path?: string;
  description?: string;
}

interface PlaybookDetail {
  name: string;
  path?: string;
  description?: string;
  data?: Record<string, unknown>;
  raw?: string;
}

function parseGraphFromPlaybook(pb: PlaybookDetail): WorkerGraph {
  const data = pb.data ?? {};
  const stepsRaw = (data.steps as Record<string, unknown>) ?? {};
  const linksRaw = (data.links as Array<Record<string, unknown>>) ?? [];

  const nodes: WorkerStepNode[] = Object.entries(stepsRaw).map(([id, raw]) => {
    const s = (raw as Record<string, unknown>) ?? {};
    return {
      id,
      label: id,
      role: String(s.role ?? ""),
      assignment: String(s.assignment ?? ""),
      prompt: String(s.prompt ?? ""),
      capacity: Number(s.capacity ?? 1),
      timeout: s.timeout != null ? Number(s.timeout) : null,
      inputs: (s.inputs as string[]) ?? [],
      outputs: (s.outputs as string[]) ?? [],
    };
  });

  const edges: WorkerLinkEdge[] = linksRaw.map((l, i) => {
    const rawMode = String(l.mode ?? "simple");
    const mode: "simple" | "code" = rawMode === "code" ? "code" : "simple";
    return {
      id: `e-${i}`,
      source: String(l.from ?? ""),
      target: String(l.to ?? ""),
      mode,
      condition: l.condition != null ? String(l.condition) : undefined,
      map: (l.map as Record<string, string>) ?? undefined,
      handler: l.handler != null ? String(l.handler) : undefined,
    };
  });

  return {
    name: pb.name,
    description: String(data.description ?? pb.description ?? ""),
    nodes,
    edges,
  };
}

export async function listWorkers(): Promise<{ workers: WorkerSummary[] }> {
  const data = await fetchJson<{ playbooks: PlaybookListEntry[] }>("/api/playbooks/");
  return {
    workers: (data.playbooks ?? []).map((p) => ({
      name: p.name,
      file: p.path,
      description: p.description,
      steps: 0,
      links: 0,
    })),
  };
}

export async function getWorkerGraph(name: string): Promise<WorkerGraph> {
  const data = await fetchJson<PlaybookDetail>(`/api/playbooks/${encodeURIComponent(name)}`);
  return parseGraphFromPlaybook(data);
}

export async function getWorkerRaw(name: string): Promise<WorkerRaw> {
  return fetchJson<WorkerRaw>(`/api/playbooks/${encodeURIComponent(name)}`);
}

export async function createWorker(name: string, data: WorkerFormData): Promise<unknown> {
  return fetchJson<unknown>(`/api/playbooks/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateWorker(name: string, data: WorkerFormData): Promise<unknown> {
  return fetchJson<unknown>(`/api/playbooks/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function validateWorker(
  name: string,
  data: WorkerFormData,
): Promise<{ ok: boolean; errors?: string[] }> {
  return fetchJson<{ ok: boolean; errors?: string[] }>(
    `/api/playbooks/${encodeURIComponent(name)}/validate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
}

// ─── Declarative playbook format helpers ──────────────────────────────────────

/**
 * Inspect a raw playbook payload and decide which editor to render.
 *
 * - ``graph``: has ``steps`` or ``links`` keys with content
 * - ``declarative``: has ``agent`` and/or ``prompt`` and no steps/links
 * - default for empty/new playbooks: ``declarative`` (fewer required fields)
 */
export function detectPlaybookFormat(data: Record<string, unknown>): PlaybookFormat {
  const steps = data?.steps;
  const links = data?.links;
  const hasSteps =
    steps != null && typeof steps === "object" && Object.keys(steps as object).length > 0;
  const hasLinks = Array.isArray(links) && links.length > 0;
  if (hasSteps || hasLinks) return "graph";
  return "declarative";
}

/**
 * Map raw YAML payload → DeclarativePlaybookData shape the form binds to.
 */
export function rawToDeclarative(
  name: string,
  data: Record<string, unknown>,
): DeclarativePlaybookData {
  const argsRaw = (data.args as Record<string, Record<string, unknown>>) ?? {};
  const args: DeclarativeArgSpec[] = Object.entries(argsRaw).map(([argName, spec]) => ({
    name: argName,
    type: String(spec?.type ?? "str"),
    default: spec?.default != null ? String(spec.default) : "",
    help: String(spec?.help ?? ""),
  }));

  return {
    name,
    description: String(data.description ?? ""),
    agent: String(data.agent ?? ""),
    effort: String(data.effort ?? ""),
    maxOps: data["max-ops"] != null ? Number(data["max-ops"]) : null,
    prompt: String(data.prompt ?? ""),
    args,
    yolo: Boolean(data.yolo ?? false),
    showGraph: Boolean(data["show-graph"] ?? false),
    argumentHint: String(data["argument-hint"] ?? ""),
  };
}

/**
 * Convert DeclarativePlaybookData → wire payload for PUT /api/playbooks/{name}.
 * Uses the YAML key names (with hyphens) the backend expects.
 */
export function declarativeToPayload(data: DeclarativePlaybookData): Record<string, unknown> {
  const argsOut: Record<string, Record<string, unknown>> = {};
  for (const a of data.args) {
    const trimmed = a.name.trim();
    if (!trimmed) continue;
    const spec: Record<string, unknown> = { type: a.type || "str" };
    if (a.default !== "") spec.default = a.default;
    if (a.help) spec.help = a.help;
    argsOut[trimmed] = spec;
  }

  return {
    description: data.description,
    agent: data.agent || null,
    effort: data.effort || null,
    "max-ops": data.maxOps != null && Number.isFinite(data.maxOps) ? data.maxOps : null,
    prompt: data.prompt || null,
    args: Object.keys(argsOut).length > 0 ? argsOut : null,
    yolo: data.yolo,
    "show-graph": data.showGraph,
    "argument-hint": data.argumentHint || null,
  };
}

/**
 * Generic playbook update — accepts any partial dict, lets the backend merge.
 * Use this for declarative-format saves; graph saves continue to use
 * ``updateWorker`` since the wire shape is fully typed.
 */
export async function updatePlaybook(
  name: string,
  payload: Record<string, unknown>,
): Promise<unknown> {
  return fetchJson<unknown>(`/api/playbooks/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ADR-0014: Run button is defaults-only. No task/cwd payload — the backend
// runs the playbook with its default configuration. Input binding and
// worktree customisation belong in `li play`.
export async function startRun(workerName: string): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>(`/api/playbooks/${encodeURIComponent(workerName)}/run`, {
    method: "POST",
  });
}

// ─── Agents ───────────────────────────────────────────────────────────────────

export async function listAgents(): Promise<{ agents: AgentProfileSummary[] }> {
  return fetchJson<{ agents: AgentProfileSummary[] }>("/api/agents/");
}

export async function getAgent(name: string): Promise<AgentProfile> {
  return fetchJson<AgentProfile>(`/api/agents/${encodeURIComponent(name)}`);
}

export async function createAgent(name: string, data: AgentProfile): Promise<unknown> {
  return fetchJson<unknown>(`/api/agents/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateAgent(name: string, data: AgentProfile): Promise<unknown> {
  return fetchJson<unknown>(`/api/agents/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

// ─── Shows ────────────────────────────────────────────────────────────────────

export async function listShows(): Promise<ShowSummary[]> {
  return fetchJson<ShowSummary[]>("/api/shows/");
}

export async function getShow(topic: string): Promise<ShowDetail> {
  return fetchJson<ShowDetail>(`/api/shows/${encodeURIComponent(topic)}`);
}

// H-FE-5: terminal {"type":"done"} event from shows.py MUST close the
// stream. The closer runs BEFORE invoking the callback for done events so
// that close() always runs even if the callback throws.
export function streamShow(topic: string, onEvent: (event: ShowEvent) => void): () => void {
  const close = sseSubscribe(`/api/shows/${encodeURIComponent(topic)}/stream`, (data) => {
    const event = JSON.parse(data) as ShowEvent;
    if (event.type === "done") {
      close();
    }
    onEvent(event);
  });
  return close;
}

// ─── Sessions ────────────────────────────────────────────────────────────────

export interface SessionSummary {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  branch_count: number;
  message_count: number;
  status: string;
}

export interface SessionMessage {
  id: string;
  role: string;
  content: Record<string, unknown>;
  sender: string | null;
  timestamp: number;
  lion_class: string;
  branch_id?: string;
}

export interface SessionBranch {
  id: string;
  name: string;
  created_at: number;
  messages: SessionMessage[];
  model?: string | null;
  provider?: string | null;
  agent_name?: string | null;
}

export interface SessionDetail {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  branches: SessionBranch[];
  // ADR-0022: provenance disclosure — mirrors what list_sessions() exposes.
  model?: string | null;
  provider?: string | null;
  effort?: string | null;
  agent_hash?: string | null;
  invocation_id?: string | null;
  // ADR-0029: artifact contract and verification result.
  artifact_contract_json?: ArtifactContract | null;
  artifact_verification_json?: ArtifactVerification | null;
}

export async function listSessions(): Promise<{ sessions: SessionSummary[] }> {
  return fetchJson<{ sessions: SessionSummary[] }>("/api/sessions/");
}

export async function getSession(id: string): Promise<SessionDetail> {
  return fetchJson<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`);
}

export function streamSession(
  id: string,
  onEvent: (event: Record<string, unknown>) => void,
): () => void {
  const close = sseSubscribe(`/api/sessions/${encodeURIComponent(id)}/stream`, (data) => {
    let event: Record<string, unknown>;
    try {
      event = JSON.parse(data) as Record<string, unknown>;
    } catch {
      /* malformed chunk */
      return;
    }
    if (event.type === "done") {
      close();
    }
    onEvent(event);
  });
  return close;
}

// ─── Session lifecycle signals (Phase C Move 1) ───────────────────────────────

export interface SignalEvent {
  id: string;
  session_id: string;
  seq: number;
  kind: string;
  op_id: string;
  ts: number;
  payload: Record<string, unknown>;
}

export function streamSignals(
  id: string,
  onEvent: (event: SignalEvent | { type: string }) => void,
): () => void {
  const close = sseSubscribe(`/api/sessions/${encodeURIComponent(id)}/signals`, (data) => {
    let event: SignalEvent | { type: string };
    try {
      event = JSON.parse(data) as SignalEvent | { type: string };
    } catch {
      /* malformed chunk */
      return;
    }
    if ("type" in event && event.type === "done") {
      close();
    }
    onEvent(event);
  });
  return close;
}

// ─── Invocations (ADR-0020) ───────────────────────────────────────────────────

export interface InvocationSummary {
  id: string;
  skill: string;
  plugin: string | null;
  prompt: string | null;
  started_at: number;
  ended_at: number | null;
  status: string;
  session_count: number;
  created_at: number;
  updated_at: number;
  node_metadata: Record<string, unknown> | null;
  // ADR-0026: project provenance from the most-recently updated child session.
  project?: string | null;
  project_source?: string | null;
}

export interface InvocationSession {
  id: string;
  name: string | null;
  agent_name: string | null;
  playbook_name: string | null;
  invocation_kind: string | null;
  status: string | null;
  last_message_at: number | null;
  started_at: number | null;
  ended_at: number | null;
  // ADR-0022: per-child-session model + effort disclosure.
  model?: string | null;
  effort?: string | null;
}

// ADR-0021: structured skill outputs. `kind` is the dispatch key for
// the frontend renderer; `content` shape depends on the kind.
export interface ArtifactSummary {
  id: string;
  invocation_id: string | null;
  session_id: string | null;
  kind: string;
  name: string;
  created_at: number;
  content: Record<string, unknown> | null;
  file_path: string | null;
}

export interface InvocationDetail extends InvocationSummary {
  sessions: InvocationSession[];
  artifacts: ArtifactSummary[];
}

export async function getArtifact(id: string): Promise<ArtifactSummary> {
  return fetchJson<ArtifactSummary>(`/api/artifacts/${encodeURIComponent(id)}`);
}

export async function listArtifactsForSession(
  sessionId: string,
): Promise<{ artifacts: ArtifactSummary[] }> {
  return fetchJson<{ artifacts: ArtifactSummary[] }>(
    `/api/artifacts/by-session/${encodeURIComponent(sessionId)}`,
  );
}

export interface InvocationListResponse {
  invocations: InvocationSummary[];
  limit: number;
  offset: number;
  has_next: boolean;
}

export interface InvocationListParams {
  skill?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export async function listInvocations(
  params?: InvocationListParams,
): Promise<InvocationListResponse> {
  const query = new URLSearchParams();
  if (params?.skill) query.set("skill", params.skill);
  if (params?.status) query.set("status", params.status);
  if (params?.limit !== undefined) query.set("limit", String(params.limit));
  if (params?.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return fetchJson<InvocationListResponse>(`/api/invocations/${qs ? `?${qs}` : ""}`);
}

export async function getInvocation(id: string): Promise<InvocationDetail> {
  return fetchJson<InvocationDetail>(`/api/invocations/${encodeURIComponent(id)}`);
}

// ─── Definitions (versioned md files via SQLite) ──────────────────────────────

export interface DefinitionSummary {
  kind: string;
  name: string;
  path: string;
  disk_path: string;
  has_versions: boolean;
  version: number;
  updated_at: number;
}

export interface DefinitionVersion {
  id: string;
  version: number;
  created_at: number;
  message: string | null;
}

export interface DefinitionDetail {
  kind: string;
  name: string;
  path: string;
  content: string;
  version: number;
  versions: DefinitionVersion[];
}

export async function listDefinitions(
  kind?: string,
): Promise<{ definitions: DefinitionSummary[] }> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  return fetchJson<{ definitions: DefinitionSummary[] }>(`/api/definitions${query}`);
}

export async function getDefinition(kind: string, name: string): Promise<DefinitionDetail> {
  return fetchJson<DefinitionDetail>(
    `/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`,
  );
}

export async function getDefinitionVersion(
  kind: string,
  name: string,
  version: number,
): Promise<DefinitionDetail> {
  return fetchJson<DefinitionDetail>(
    `/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}/versions/${version}`,
  );
}

// F-A3-1 (ADR-0016): backend is POST /api/definitions/{kind}/{name} — no PUT route exists.
// Return type matches services/definitions.py save_definition() response shape:
//   { kind, name, version, saved_at, message? }
export async function saveDefinition(
  kind: string,
  name: string,
  content: string,
  message?: string,
): Promise<{
  kind: string;
  name: string;
  version: number;
  saved_at: number;
  message: string | null;
}> {
  return fetchJson<{
    kind: string;
    name: string;
    version: number;
    saved_at: number;
    message: string | null;
  }>(`/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, message }),
  });
}

// H-FE-4: version is a query param per ADR-0016 and definitions.py:58-63,
// not a path segment. Return type updated to include full rollback response.
export async function rollbackDefinition(
  kind: string,
  name: string,
  version: number,
): Promise<{
  version: number;
  saved_at: number;
  rolled_back_from: number;
  rolled_back_to: number;
  message: string | null;
}> {
  return fetchJson(
    `/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}/rollback?version=${version}`,
    { method: "POST" },
  );
}

export async function snapshotDefinitions(kind?: string): Promise<{ snapshots_created: number }> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  return fetchJson<{ snapshots_created: number }>(`/api/definitions/snapshot${query}`, {
    method: "POST",
  });
}

// ─── Skills ─────────────────────────────────────────────────────────────────

export interface SkillSummary {
  name: string;
  description: string;
  path: string;
  allowed_tools: string[];
}

export interface SkillDetail {
  name: string;
  description: string;
  path: string;
  content: string;
  allowed_tools: string[];
}

export async function listSkills(): Promise<{ skills: SkillSummary[] }> {
  return fetchJson<{ skills: SkillSummary[] }>("/api/skills/");
}

export async function getSkill(name: string): Promise<SkillDetail> {
  return fetchJson<SkillDetail>(`/api/skills/${encodeURIComponent(name)}`);
}

// ─── Plugins ──────────────────────────────────────────────────────────────────

export interface PluginSummary {
  name: string;
  description: string;
  version: string;
  source: "marketplace" | "third-party";
  skill_count: number;
  agent_count: number;
  has_hooks: boolean;
  has_mcp: boolean;
  path: string;
}

export interface PluginSkillRef {
  name: string;
  description: string;
}

export interface PluginAgentRef {
  name: string;
  description: string;
}

export interface PluginDetail {
  name: string;
  description: string;
  version: string;
  source: "marketplace" | "third-party";
  skill_count: number;
  agent_count: number;
  has_hooks: boolean;
  has_mcp: boolean;
  path: string;
  skills: PluginSkillRef[];
  agents: PluginAgentRef[];
  hooks: Record<string, unknown> | null;
  mcp: Record<string, unknown> | null;
  readme: string | null;
}

export interface PluginSkillDetail {
  name: string;
  description: string;
  path: string;
  content: string;
  allowed_tools: string[];
}

export async function listPlugins(): Promise<{ plugins: PluginSummary[] }> {
  return fetchJson<{ plugins: PluginSummary[] }>("/api/plugins/");
}

export async function getPlugin(name: string): Promise<PluginDetail> {
  return fetchJson<PluginDetail>(`/api/plugins/${encodeURIComponent(name)}`);
}

export async function getPluginSkill(
  pluginName: string,
  skillName: string,
): Promise<PluginSkillDetail> {
  return fetchJson<PluginSkillDetail>(
    `/api/plugins/${encodeURIComponent(pluginName)}/skills/${encodeURIComponent(skillName)}`,
  );
}

// ─── Admin ────────────────────────────────────────────────────────────────────

export type PhantomReason = "process_dead" | "missing_artifacts" | "stale_lock";

export interface PhantomSession {
  session_id: string;
  playbook: string | null;
  started_at: number | null;
  reason: PhantomReason;
}

export interface AdminDoctorResponse {
  phantom_sessions: PhantomSession[];
  db_health: {
    size_bytes: number;
    wal_bytes: number;
    wal_pending: number;
  };
  diagnostic_run_at: string;
}

export interface AdminPruneRequest {
  session_ids?: string[];
  all_phantom?: boolean;
}

export async function getAdminDoctor(): Promise<AdminDoctorResponse> {
  return fetchJson<AdminDoctorResponse>("/api/admin/doctor");
}

export async function pruneAdmin(body: AdminPruneRequest): Promise<{ pruned: number }> {
  return fetchJson<{ pruned: number }>("/api/admin/prune", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ─── Admin maintenance (Phase C Move 3) ──────────────────────────────────────

export type MaintenanceAction = "vacuum" | "checkpoint" | "prune";

export interface MaintenanceResult {
  action: MaintenanceAction;
  // vacuum
  status?: string;
  // checkpoint
  mode?: string;
  busy?: number | null;
  log_pages?: number | null;
  checkpointed?: number | null;
  // prune
  sessions_pruned?: number;
  runs_pruned?: number;
}

export async function runMaintenance(action: MaintenanceAction): Promise<MaintenanceResult> {
  return fetchJson<MaintenanceResult>("/api/admin/maintenance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
}

// ─── Projects (ADR-0026) ──────────────────────────────────────────────────────

export interface ProjectListResponse {
  projects: ProjectSummary[];
  unassigned_count: number;
}

export async function listProjects(): Promise<ProjectListResponse> {
  return fetchJson<ProjectListResponse>("/api/projects/");
}

export async function getProject(name: string): Promise<ProjectDetail> {
  return fetchJson<ProjectDetail>(`/api/projects/${encodeURIComponent(name)}`);
}

export async function createProject(data: {
  name: string;
  github?: string;
  description?: string;
  path?: string;
}): Promise<unknown> {
  return fetchJson<unknown>("/api/projects/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateProject(
  name: string,
  data: { github?: string; description?: string; path?: string },
): Promise<unknown> {
  return fetchJson<unknown>(`/api/projects/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteProject(name: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/projects/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ─── Teams ────────────────────────────────────────────────────────────────────

export interface TeamSummary {
  id: string;
  name: string;
  member_count: number;
  last_modified: number;
}

export interface TeamListResponse {
  teams: TeamSummary[];
  limit: number;
  offset: number;
  total: number;
  has_next: boolean;
}

export type TeamDetail = Record<string, unknown> & {
  id?: string;
  name?: string;
  members?: unknown[];
  messages?: unknown[];
  created_at?: string | number | null;
};

export async function listTeams(params?: {
  limit?: number;
  offset?: number;
}): Promise<TeamListResponse> {
  const query = new URLSearchParams();
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.offset != null) query.set("offset", String(params.offset));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return fetchJson<TeamListResponse>(`/api/teams${suffix}`);
}

export async function getTeam(teamId: string): Promise<TeamDetail> {
  return fetchJson<TeamDetail>(`/api/teams/${encodeURIComponent(teamId)}`);
}

// ─── Stats (extended) ─────────────────────────────────────────────────────────

export interface DbStats {
  path: string;
  size_bytes: number;
  wal_bytes: number;
  connections_active: number;
  last_checkpoint_at: string | null;
  tables?: Record<string, number>;
  sessions_by_status?: Record<string, number>;
  pragmas?: Record<string, string | number | boolean | null>;
  slow_queries?: null;
}

export interface StudioStats {
  playbooks: number;
  agents: number;
  runs: number;
  shows: number;
  skills: number;
  plugins: number;
  db?: DbStats;
}

export async function getStats(): Promise<StudioStats> {
  return fetchJson<StudioStats>("/api/stats/");
}

// ─── Schedules (ADR-0027) ───────────────────────────────────────────────────

export interface ScheduleListResponse {
  schedules: ScheduleSummary[];
}

export async function listSchedules(params?: {
  enabled?: boolean;
  trigger_type?: string;
  project?: string;
}): Promise<ScheduleListResponse> {
  const query = new URLSearchParams();
  if (params?.enabled !== undefined) query.set("enabled", String(params.enabled));
  if (params?.trigger_type) query.set("trigger_type", params.trigger_type);
  if (params?.project) query.set("project", params.project);
  const qs = query.toString();
  return fetchJson<ScheduleListResponse>(`/api/schedules/${qs ? `?${qs}` : ""}`);
}

export async function getSchedule(id: string): Promise<ScheduleDetail> {
  return fetchJson<ScheduleDetail>(`/api/schedules/${encodeURIComponent(id)}`);
}

export async function createSchedule(
  data: Record<string, unknown>,
): Promise<{ id: string; name: string }> {
  return fetchJson<{ id: string; name: string }>("/api/schedules/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateSchedule(id: string, data: Record<string, unknown>): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteSchedule(id: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function enableSchedule(id: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}/enable`, {
    method: "POST",
  });
}

export async function disableSchedule(id: string): Promise<unknown> {
  return fetchJson<unknown>(`/api/schedules/${encodeURIComponent(id)}/disable`, {
    method: "POST",
  });
}

export async function triggerSchedule(id: string): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>(`/api/schedules/${encodeURIComponent(id)}/trigger`, {
    method: "POST",
  });
}

export async function listScheduleRuns(
  scheduleId: string,
  params?: { status?: string; limit?: number; offset?: number },
): Promise<{ runs: ScheduleRunSummary[]; has_next: boolean }> {
  const query = new URLSearchParams();
  if (params?.status) query.set("status", params.status);
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return fetchJson(`/api/schedules/${encodeURIComponent(scheduleId)}/runs${qs ? `?${qs}` : ""}`);
}

// ─── Engine runs (Phase C Move 2) ─────────────────────────────────────────────

export interface EngineRunSummary {
  id: string;
  kind: string;
  spec_json: Record<string, unknown>;
  status: string;
  started_at: number;
  ended_at: number | null;
  session_id: string | null;
  export_dir: string | null;
  error: string | null;
}

export interface EngineRunListParams {
  kind?: string;
  status?: string;
  session_id?: string;
  limit?: number;
  offset?: number;
}

export async function listEngineRuns(params?: EngineRunListParams): Promise<EngineRunSummary[]> {
  const query = new URLSearchParams();
  if (params?.kind) query.set("kind", params.kind);
  if (params?.status) query.set("status", params.status);
  if (params?.session_id) query.set("session_id", params.session_id);
  if (params?.limit != null) query.set("limit", String(params.limit));
  if (params?.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return fetchJson<EngineRunSummary[]>(`/api/engine-runs${qs ? `?${qs}` : ""}`);
}

export async function getEngineRun(runId: string): Promise<EngineRunSummary> {
  return fetchJson<EngineRunSummary>(`/api/engine-runs/${encodeURIComponent(runId)}`);
}

// ─── Engine definitions ───────────────────────────────────────────────────────

export interface EngineDef {
  id: string;
  name: string;
  kind: string;
  model: string | null;
  max_depth: number | null;
  max_agents: number | null;
  options: Record<string, string> | null;
  description: string | null;
  created_at: number;
  updated_at: number;
}

export interface CreateEngineDefRequest {
  name: string;
  kind: string;
  model?: string;
  max_depth?: number;
  max_agents?: number;
  options?: Record<string, string>;
  description?: string;
}

export interface UpdateEngineDefRequest {
  name?: string;
  kind?: string;
  model?: string;
  max_depth?: number;
  max_agents?: number;
  options?: Record<string, string>;
  description?: string;
}

export interface LaunchResult {
  invocation_id: string;
  engine_def_id: string;
  engine_def_name: string;
  kind: string;
  argv: string[];
}

export async function listEngineDefs(params?: { kind?: string }): Promise<EngineDef[]> {
  const query = new URLSearchParams();
  if (params?.kind) query.set("kind", params.kind);
  const qs = query.toString();
  return fetchJson<EngineDef[]>(`/api/engine-defs/${qs ? `?${qs}` : ""}`);
}

export async function getEngineDef(defId: string): Promise<EngineDef> {
  return fetchJson<EngineDef>(`/api/engine-defs/${encodeURIComponent(defId)}`);
}

export async function createEngineDef(
  body: CreateEngineDefRequest,
): Promise<{ id: string; name: string; created_at: number }> {
  return fetchJson(`/api/engine-defs/`, { method: "POST", body: JSON.stringify(body) });
}

export async function updateEngineDef(
  defId: string,
  body: UpdateEngineDefRequest,
): Promise<{ ok: boolean }> {
  return fetchJson(`/api/engine-defs/${encodeURIComponent(defId)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function deleteEngineDef(defId: string): Promise<{ ok: boolean }> {
  return fetchJson(`/api/engine-defs/${encodeURIComponent(defId)}`, { method: "DELETE" });
}

export async function launchEngine(body: {
  action_kind: "engine";
  action_engine_def: string;
  action_prompt: string;
}): Promise<LaunchResult> {
  return fetchJson(`/api/launches/`, { method: "POST", body: JSON.stringify(body) });
}
