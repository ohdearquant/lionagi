// ─── Run types ───────────────────────────────────────────────────────────────

export interface RunSummary {
  run_id: string;
  state_root: string;
  artifact_root: string;
  worker_name: string;
  task: string;
  status: string;
  step_count: number;
  started_at: number | null;
  finished_at: number | null;
  model?: string;
}

export interface RunMessage {
  role: string;
  content?: string;
  sender?: string;
  timestamp?: number | null;
  function?: string;
  summary?: string;
  arguments?: Record<string, unknown>;
  output?: string;
  status?: string;
  exit_code?: number | null;
}

export interface RunStep {
  step: string;
  status: string;
  result?: Record<string, unknown>;
  messages?: RunMessage[];
  timestamp: number | null;
}

export interface RunDetail {
  run_id: string;
  state_root: string;
  artifact_root: string;
  worker_name: string;
  task: string;
  status: string;
  error: string | null;
  cwd: string | null;
  started_at: number | null;
  finished_at: number | null;
  steps?: RunStep[];
  graph: { nodes: WorkerStepNode[]; edges: WorkerLinkEdge[] };
  manifest: Record<string, unknown>;
  branches: unknown[];
}

// ─── Worker / Playbook types ──────────────────────────────────────────────────

export interface WorkerSummary {
  name: string;
  file?: string;
  description?: string;
  steps: number;
  links: number;
}

export interface WorkerStepNode {
  id: string;
  label: string;
  role: string;
  assignment: string;
  prompt: string;
  capacity: number;
  timeout: number | null;
  inputs: string[];
  outputs: string[];
}

export interface WorkerLinkEdge {
  id: string;
  source: string;
  target: string;
  mode: "simple" | "code";
  condition?: string;
  map?: Record<string, string>;
  handler?: string;
}

export interface WorkerGraph {
  name: string;
  description: string;
  nodes: WorkerStepNode[];
  edges: WorkerLinkEdge[];
}

export interface WorkerRaw {
  name: string;
  path?: string;
  description?: string;
  use?: { models?: Record<string, ModelConfig> };
  data?: Record<string, unknown>;
  raw?: string;
}

// ─── Declarative playbook (agent + prompt format) ─────────────────────────────

export type PlaybookFormat = "declarative" | "graph";

export interface DeclarativeArgSpec {
  name: string;
  type: string;
  default: string;
  help: string;
}

export interface DeclarativePlaybookData {
  name: string;
  description: string;
  agent: string;
  effort: string;
  maxOps: number | null;
  prompt: string;
  args: DeclarativeArgSpec[];
  yolo: boolean;
  showGraph: boolean;
  argumentHint: string;
}

export interface WorkerFormData {
  name: string;
  description: string;
  use: { models: Record<string, ModelConfig> };
  steps: Record<
    string,
    {
      assignment: string;
      role: string;
      prompt: string;
      capacity?: number;
      timeout?: number | null;
    }
  >;
  links: Array<{
    from: string;
    to: string;
    condition?: string;
    map?: Record<string, string>;
    handler?: string;
  }>;
}

// ─── Agent types ──────────────────────────────────────────────────────────────

export interface AgentProfileSummary {
  name: string;
  description?: string;
  provider: string;
  model: string;
}

export interface AgentProfile {
  name: string;
  path: string;
  provider: string;
  model: string;
  system_prompt: string | null;
  guidance: string | null;
  permission_mode?: string;
  reasoning_effort?: string;
  description?: string;
}

// ─── Model config ─────────────────────────────────────────────────────────────

export interface ModelConfig {
  provider: string;
  model: string;
  reasoning_effort?: string;
  permission_mode?: string;
}

// ─── Show types ───────────────────────────────────────────────────────────────

export interface ShowSummary {
  topic: string;
  play_count: number;
  latest_status: string;
  last_update: number | string | null;
}

export interface PlayMeta {
  worktree?: string;
  branch: string;
  status: string;
  attempt: number;
  started_at: string;
  ended_at?: string;
  exit_code?: number;
  merged_at?: string;
  merge_sha?: string;
  team_missing?: boolean;
}

export interface ShowVerdict {
  gate_passed: boolean;
  feedback?: string | null;
  notes?: string | null;
}

export interface ShowDetail {
  topic: string;
  path?: string;
  show_md: string | null;
  plays: Array<{
    name: string;
    meta: PlayMeta;
    verdict?: ShowVerdict | null;
    updated_at?: number | string | null;
  }>;
}

export interface ShowEvent {
  type: "new" | "change" | "delete";
  path: string;
  size?: number;
}
