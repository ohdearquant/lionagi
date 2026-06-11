// ─── Project types (ADR-0026) ────────────────────────────────────────────────

export interface ProjectSummary {
  name: string;
  source: string;
  path: string | null;
  github: string | null;
  description: string | null;
  session_count: number;
  running_count: number;
  editable: boolean;
  created_at: number;
  updated_at: number;
  last_seen_at: number | null;
}

export interface ProjectDetail extends ProjectSummary {
  agents_used: Array<{ agent_name: string; run_count: number }>;
  playbooks_used: Array<{ playbook_name: string; run_count: number }>;
}

// ─── Artifact contract types (ADR-0029) ─────────────────────────────────────

export interface ExpectedArtifact {
  id: string;
  path: string;
  required?: boolean;
  description?: string;
  source?: string;
}

export interface ProducedArtifact {
  id: string;
  path: string;
  size: number;
  present?: boolean;
}

export interface ArtifactContract {
  expected: ExpectedArtifact[];
}

export interface ArtifactVerification {
  status: "passed" | "failed" | "warning" | "skipped";
  checked_at: number;
  missing_required: ExpectedArtifact[];
  missing_optional: ExpectedArtifact[];
  produced: ProducedArtifact[];
}

// ─── Run types ───────────────────────────────────────────────────────────────

// H-FE-3: RunSummary matches the actual SQLite-session response shape from
// list_runs() (services/runs.py). Fields worker_name/finished_at were stale
// filesystem-run remnants; the real fields are playbook_name/ended_at etc.
export interface RunSummary {
  run_id: string;
  id?: string;
  name?: string | null;
  playbook_name?: string | null;
  agent_name?: string | null;
  invocation_kind?: string | null;
  show_topic?: string | null;
  show_play_name?: string | null;
  source_kind?: string;
  status: string;
  // ADR-0024: derived health indicator computed at read time.
  // - healthy / idle: alive and active (or quietly waiting).
  // - unresponsive: alive but past kind-aware threshold.
  // - stale: process dead, has produced output.
  // - orphaned: process dead, no output, no artifacts.
  // - zombie: terminal status, but resources leaked (stale locks).
  effective_health?: "healthy" | "idle" | "unresponsive" | "stale" | "orphaned" | "zombie" | null;
  last_message_at?: number | null;
  // ADR-0020: optional parent skill orchestration id (from `li invoke`).
  invocation_id?: string | null;
  // ADR-0022: provenance disclosure. `model` is the resolved
  // "provider/name" spec, `provider` is the raw provider key, `effort`
  // is the run's effort level (low/medium/high/xhigh), `agent_hash` is
  // a 16-char fingerprint of the agent profile content at run time.
  model?: string | null;
  provider?: string | null;
  effort?: string | null;
  agent_hash?: string | null;
  started_at: number | null;
  ended_at?: number | null;
  created_at?: number | null;
  updated_at?: number | null;
  branch_count?: number;
  message_count?: number;
  // ADR-0026: project detection for session organization.
  project?: string | null;
  project_source?: string | null;
  // ADR-0029: artifact contract and verification result.
  artifact_contract_json?: ArtifactContract | null;
  artifact_verification_json?: ArtifactVerification | null;
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

// RunDetail comes from the filesystem run.json path (GET /api/runs/{id} →
// services/runs.py get_run → _adapt_summary). Unlike RunSummary which maps
// SQLite session rows, RunDetail reads the on-disk run manifest. The manifest
// uses "worker_name" and "finished_at" as canonical field names (see
// _adapt_summary in services/runs.py). ADR-0004 open design question:
// long-term these should unify with the SQLite session fields.
export interface RunDetail {
  run_id: string;
  state_root: string;
  artifact_root: string;
  // Filesystem run.json fields — distinct from SQLite session schema
  worker_name?: string;
  task?: string;
  status: string;
  error: string | null;
  cwd: string | null;
  started_at: number | null;
  finished_at?: number | null;
  ended_at?: number | null;
  steps?: RunStep[];
  graph: { nodes: WorkerStepNode[]; edges: WorkerLinkEdge[] };
  manifest: Record<string, unknown>;
  branches: unknown[];
  // ADR-0029: artifact contract and verification result.
  artifact_contract_json?: ArtifactContract | null;
  artifact_verification_json?: ArtifactVerification | null;
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
  goal?: string | null;
  status?: string;
  // M-FE-2: status_source added by backend agent (H-BE-3)
  status_source?: "sqlite" | "filesystem";
  plays: Array<{
    name: string;
    meta: PlayMeta;
    verdict?: ShowVerdict | null;
    updated_at?: number | string | null;
    session_id?: string | null;
    session_name?: string | null;
    intent?: string | null;
    depends_on?: string[];
  }>;
}

// H-FE-5: "done" is the terminal SSE event emitted by shows.py:456-458.
// The SSE subscription MUST be closed when this event arrives.
export interface ShowEvent {
  type: "new" | "change" | "delete" | "done";
  path?: string;
  size?: number;
}

// ─── Schedule types (ADR-0027) ───────────────────────────────────────────────

export interface ScheduleSummary {
  id: string;
  name: string;
  description: string | null;
  enabled: number;
  trigger_type: "cron" | "interval" | "github_poll";
  cron_expr: string | null;
  interval_sec: number | null;
  github_repo: string | null;
  poll_interval_sec: number | null;
  action_kind: "agent" | "flow" | "fanout" | "play";
  action_model: string | null;
  action_prompt: string | null;
  action_agent: string | null;
  action_playbook: string | null;
  action_project: string | null;
  on_success: Record<string, unknown> | null;
  on_fail: Record<string, unknown> | null;
  last_fired_at: number | null;
  next_fire_at: number | null;
  missed_fire_policy: string;
  overlap_policy: string;
  project: string | null;
  created_at: number;
  updated_at: number;
}

export interface ScheduleRunSummary {
  id: string;
  schedule_id: string;
  invocation_id: string | null;
  trigger_context: Record<string, unknown>;
  action_kind: string;
  status: "running" | "completed" | "failed" | "skipped" | "cancelled";
  exit_code: number | null;
  chain_depth: number;
  fired_at: number;
  ended_at: number | null;
  error_detail: string | null;
}

export interface ScheduleDetail extends ScheduleSummary {
  recent_runs: ScheduleRunSummary[];
}
