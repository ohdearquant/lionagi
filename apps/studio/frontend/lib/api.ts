import type {
  AgentProfile,
  AgentProfileSummary,
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

export const API_BASE =
  process.env.NEXT_PUBLIC_STUDIO_API_BASE ?? "http://localhost:8765";

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

// ─── Runs ─────────────────────────────────────────────────────────────────────

export async function listRuns(
  params?: Record<string, string>,
): Promise<{ runs: RunSummary[] }> {
  const query =
    params && Object.keys(params).length > 0
      ? `?${new URLSearchParams(params).toString()}`
      : "";
  return fetchJson<{ runs: RunSummary[] }>(`/api/runs${query}`);
}

export async function getRun(runId: string): Promise<RunDetail> {
  return fetchJson<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`);
}

export async function rerunRun(runId: string): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>(
    `/api/runs/${encodeURIComponent(runId)}/rerun`,
    { method: "POST" },
  );
}

export function runEventsUrl(runId: string): string {
  return `${API_BASE}/api/runs/${encodeURIComponent(runId)}/events`;
}

export function streamRunEvents(
  runId: string,
  onEvent: (event: Record<string, unknown>) => void,
): () => void {
  const source = new EventSource(runEventsUrl(runId));
  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as Record<string, unknown>);
    } catch {
      /* malformed chunk */
    }
  };
  source.onerror = () => source.close();
  return () => source.close();
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
  const data = await fetchJson<{ playbooks: PlaybookListEntry[] }>(
    "/api/playbooks",
  );
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
  const data = await fetchJson<PlaybookDetail>(
    `/api/playbooks/${encodeURIComponent(name)}`,
  );
  return parseGraphFromPlaybook(data);
}

export async function getWorkerRaw(name: string): Promise<WorkerRaw> {
  return fetchJson<WorkerRaw>(`/api/playbooks/${encodeURIComponent(name)}`);
}

export async function createWorker(
  name: string,
  data: WorkerFormData,
): Promise<unknown> {
  return fetchJson<unknown>(`/api/playbooks/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateWorker(
  name: string,
  data: WorkerFormData,
): Promise<unknown> {
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

export async function startRun(
  workerName: string,
  task: string,
  cwd: string,
): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>(
    `/api/playbooks/${encodeURIComponent(workerName)}/run`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, cwd }),
    },
  );
}

// ─── Agents ───────────────────────────────────────────────────────────────────

export async function listAgents(): Promise<{ agents: AgentProfileSummary[] }> {
  return fetchJson<{ agents: AgentProfileSummary[] }>("/api/agents");
}

export async function getAgent(name: string): Promise<AgentProfile> {
  return fetchJson<AgentProfile>(`/api/agents/${encodeURIComponent(name)}`);
}

export async function createAgent(
  name: string,
  data: AgentProfile,
): Promise<unknown> {
  return fetchJson<unknown>(`/api/agents/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateAgent(
  name: string,
  data: AgentProfile,
): Promise<unknown> {
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

export function streamShow(
  topic: string,
  onEvent: (event: ShowEvent) => void,
): () => void {
  const source = new EventSource(
    `${API_BASE}/api/shows/${encodeURIComponent(topic)}/stream`,
  );
  source.onmessage = (message) => {
    onEvent(JSON.parse(message.data) as ShowEvent);
  };
  return () => source.close();
}
