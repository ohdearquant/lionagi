import type {
  AgentProfile,
  AgentProfileSummary,
  DeclarativeArgSpec,
  DeclarativePlaybookData,
  PlaybookFormat,
  RunDetail,
  RunSummary,
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

export const API_BASE = process.env.NEXT_PUBLIC_STUDIO_API_BASE ?? "http://localhost:8765";

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

// ─── Runs ─────────────────────────────────────────────────────────────────────

export async function listRuns(params?: Record<string, string>): Promise<{ runs: RunSummary[] }> {
  const query =
    params && Object.keys(params).length > 0 ? `?${new URLSearchParams(params).toString()}` : "";
  return fetchJson<{ runs: RunSummary[] }>(`/api/runs${query}`);
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
  const data = await fetchJson<{ playbooks: PlaybookListEntry[] }>("/api/playbooks");
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
  return fetchJson<{ agents: AgentProfileSummary[] }>("/api/agents");
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
  return fetchJson<ShowSummary[]>("/api/shows");
}

export async function getShow(topic: string): Promise<ShowDetail> {
  return fetchJson<ShowDetail>(`/api/shows/${encodeURIComponent(topic)}`);
}

// H-FE-5: terminal {"type":"done"} event from shows.py:456-458 MUST close
// the EventSource. Source is closed BEFORE invoking the callback for done
// events so that close() always runs even if the callback throws.
export function streamShow(topic: string, onEvent: (event: ShowEvent) => void): () => void {
  const source = new EventSource(`${API_BASE}/api/shows/${encodeURIComponent(topic)}/stream`);
  source.onmessage = (message) => {
    const event = JSON.parse(message.data) as ShowEvent;
    if (event.type === "done") {
      source.close();
    }
    onEvent(event);
  };
  return () => source.close();
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
}

export interface SessionDetail {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  branches: SessionBranch[];
}

export async function listSessions(): Promise<{ sessions: SessionSummary[] }> {
  return fetchJson<{ sessions: SessionSummary[] }>("/api/sessions");
}

export async function getSession(id: string): Promise<SessionDetail> {
  return fetchJson<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`);
}

export function streamSession(
  id: string,
  onEvent: (event: Record<string, unknown>) => void,
): () => void {
  const source = new EventSource(`${API_BASE}/api/sessions/${encodeURIComponent(id)}/stream`);
  source.onmessage = (msg) => {
    let event: Record<string, unknown>;
    try {
      event = JSON.parse(msg.data) as Record<string, unknown>;
    } catch {
      /* malformed chunk */
      return;
    }
    if (event.type === "done") {
      source.close();
    }
    onEvent(event);
  };
  return () => source.close();
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
): Promise<{ kind: string; name: string; version: number; saved_at: number; message: string | null }> {
  return fetchJson<{ kind: string; name: string; version: number; saved_at: number; message: string | null }>(
    `/api/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, message }),
    },
  );
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
  return fetchJson<{ skills: SkillSummary[] }>("/api/skills");
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
  return fetchJson<{ plugins: PluginSummary[] }>("/api/plugins");
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
