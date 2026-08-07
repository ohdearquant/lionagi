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

export interface ArtifactVerificationResult {
  status: "passed" | "failed" | "warning" | "skipped";
  checked_at: number;
  missing_required: ExpectedArtifact[];
  missing_optional: ExpectedArtifact[];
  produced: ProducedArtifact[];
  /** A reading taken while the run is still going, rather than the recorded
   *  verdict it was judged on. Artifacts may still appear. */
  provisional?: boolean;
}

export interface ArtifactVerificationNotRecorded {
  /** The run is terminal, but no verifier verdict was persisted. */
  status: "not_recorded";
}

export type ArtifactVerification = ArtifactVerificationResult | ArtifactVerificationNotRecorded;

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
  // ADR-0028: denormalized status reason (machine code + one-line summary).
  status_reason_code?: string | null;
  status_reason_summary?: string | null;
  // ADR-0029: artifact contract and verification result.
  artifact_contract_json?: ArtifactContract | null;
  artifact_verification_json?: ArtifactVerification | null;
  // Cost-visibility contract: `null` means the provider never reported a
  // cost for this run (unknown); a genuine `0` is a distinct, real value.
  // Never coerce one into the other — format with usageFormat.ts.
  total_cost_usd?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
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

export interface RunResumeRequest {
  instruction: string;
  branch_id?: string;
  model?: string;
}

export interface RunResumeResponse {
  run_id: string;
  branch_id: string;
  invocation_id: string;
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
  /** Cast role this agent wraps (e.g. "critic"), if any -- see /api/casts/. */
  role?: string;
  /** Cognitive mode overlay (e.g. "terse"), if any -- see /api/casts/. */
  mode?: string;
  /** True for a catalog-sourced/hand-marked agent: not editable or deletable. */
  protected?: boolean;
  /** True for the single always-present fallback agent: not deletable. */
  is_default?: boolean;
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
  role?: string;
  mode?: string;
  protected?: boolean;
  is_default?: boolean;
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
  github_filter?: { event?: string; base?: string; state?: string } | null;
  consecutive_failures?: number;
  last_status?: string | null;
  /** Server-computed verdict from cadence + recorded schedule_runs, never
   * from next_fire_at (a promise, not evidence). */
  health_state?: "healthy" | "failing" | "overdue" | "never-fired" | "no-evidence" | "disabled";
  health_last_outcome?: string | null;
  health_last_outcome_at?: number | null;
  health_since?: number;
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

// ─── Operator conversation protocol (ADR-0083 v1) ──────────────────────────

export type OperatorConversationStatus = "active" | "archived" | "deleted";

export interface OperatorConversation {
  id: string;
  project?: string | null;
  title?: string | null;
  status: OperatorConversationStatus;
  pinned: boolean;
  nextSequence?: number;
  activeRequestId?: string | null;
  /** The provider and model this conversation is pinned to. The daemon keeps
   * using them for a turn that names neither, so the composer has to show
   * them rather than reporting "Default". */
  provider?: string | null;
  providerModel?: string | null;
  createdAt?: number;
  updatedAt?: number;
}

export type OperatorErrorCode =
  | "auth_required"
  | "validation"
  | "not_found"
  | "denied"
  | "conflict"
  | "stale_context"
  | "rate_limited"
  | "model_failure"
  | "provider_unavailable"
  | "service_failure"
  | "service_restarted"
  | "audit_unavailable"
  | "replay_gap"
  | "cancelled"
  | "protocol_version";

export interface OperatorProtocolError {
  code: OperatorErrorCode;
  message: string;
  retryable: boolean;
  retryAfterMs?: number;
  details?: Record<string, unknown>;
}

export type OperatorSpace = "mission" | "designer" | "library" | "history" | "schedules" | "system";

export type OperatorJsonValue =
  | null
  | boolean
  | number
  | string
  | OperatorJsonValue[]
  | { [key: string]: OperatorJsonValue };

export interface OperatorContextSnapshot {
  project?: string | null;
  space: OperatorSpace;
  route: string;
  selection?: Record<string, string> | null;
  filters: Record<string, OperatorJsonValue>;
  /** This page's count of views seen. Orders a report against a turn. */
  observationSeq?: number;
  /** Which page observed it. A count means nothing outside its own page. */
  observerId?: string;
}

export interface OperatorTextPayload {
  content: string;
  format: "plain" | "markdown";
  /** Additive backend field used to distinguish the persisted instruction. */
  role?: "user" | "assistant";
}

export interface OperatorToolCallPayload {
  callId: string;
  tool: string;
  arguments: Record<string, unknown>;
  mode: "read" | "draft";
}

export interface OperatorToolResultPayload {
  callId: string;
  ok: boolean;
  result?: unknown;
  error?: OperatorProtocolError;
}

export type OperatorUiEffect =
  | {
      id?: string;
      kind: "navigate";
      space: OperatorSpace;
      params: Record<string, OperatorJsonValue>;
    }
  | {
      id?: string;
      kind: "select";
      space: Exclude<OperatorSpace, "system">;
      selection: Record<string, string>;
    }
  | {
      id?: string;
      kind: "prefill";
      form: "schedule" | "workflow" | "playbook";
      values: Record<string, OperatorJsonValue>;
    }
  | {
      id?: string;
      kind: "theme";
      theme: "light" | "dark";
    };

export interface OperatorUiCommandPayload {
  effect: OperatorUiEffect;
}

export interface OperatorResourceVersion {
  kind: string;
  id: string;
  version: string;
}

export interface OperatorCommandProposal {
  id: string;
  command: Record<string, unknown>;
  commandHash: string;
  risk: "mutate" | "execute" | "admin";
  summary: string;
  target?: OperatorResourceVersion | null;
  idempotencyKey: string;
  expiresAt: number;
}

export interface OperatorProposalPayload {
  proposal: OperatorCommandProposal;
}

export interface OperatorConfirmationPayload {
  proposalId: string;
  state: "required" | "confirmed" | "denied" | "cancelled" | "expired" | "executed";
}

export interface OperatorErrorPayload {
  error: OperatorProtocolError;
}

export interface OperatorDonePayload {
  outcome: "completed" | "failed" | "cancelled";
  lastSequence: number;
}

export type OperatorFrameType =
  | "text"
  | "tool_call"
  | "tool_result"
  | "ui_command"
  | "proposal"
  | "confirmation"
  | "error"
  | "done";

export type OperatorPayload =
  | OperatorTextPayload
  | OperatorToolCallPayload
  | OperatorToolResultPayload
  | OperatorUiCommandPayload
  | OperatorProposalPayload
  | OperatorConfirmationPayload
  | OperatorErrorPayload
  | OperatorDonePayload;

export interface OperatorFrame<T extends OperatorPayload = OperatorPayload> {
  version: 1;
  conversationId: string;
  requestId: string;
  sequence: number;
  type: OperatorFrameType;
  payload: T;
  createdAt: number;
}

export interface OperatorConversationSnapshot {
  conversation: OperatorConversation;
  frames: OperatorFrame[];
}

/** Provider each catalog model runs through; mirrors lionagi/studio/operator/catalog.py. */
export type OperatorProvider = "claude_code" | "codex" | "gemini_code";

/** Reasoning-effort vocabulary; which subset a given provider accepts is
 * carried per-entry in OperatorModelCatalogEntry.efforts, not hardcoded here. */
export type OperatorEffort =
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max"
  | "ultra";

/** One entry in the backend-served model catalog (GET /api/operator/models).
 * The model reaches a CLI argument on the daemon, so the UI only ever offers
 * ids the server actually named -- see fetchOperatorModelCatalog. */
export interface OperatorModelCatalogEntry {
  id: string;
  label: string;
  provider: OperatorProvider;
  efforts: OperatorEffort[];
}

export interface OperatorModelCatalog {
  models: OperatorModelCatalogEntry[];
}

export interface OperatorTurnRequest {
  instruction: string;
  context: OperatorContextSnapshot;
  expectedLastSequence: number;
  model?: string;
  provider?: OperatorProvider;
  effort?: OperatorEffort;
  // Omitting `model` keeps the conversation's stored pin, so it cannot also
  // mean "drop it". This asks for the pin to be removed.
  clearSelection?: boolean;
}

export interface OperatorTurnAccepted {
  conversationId: string;
  requestId: string;
  acceptedSequence: number;
}

export interface OperatorProposalResult {
  proposalId: string;
  status: "succeeded" | "failed" | "conflict" | "expired" | "executing";
  result?: Record<string, OperatorJsonValue> | null;
  error?: OperatorProtocolError | null;
}
