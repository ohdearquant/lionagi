/** A single run record returned by GET /api/runs/ and GET /api/runs/{run_id}. */
export interface Run {
  run_id: string;
  id: string;
  name: string | null;
  playbook_name: string | null;
  agent_name: string | null;
  invocation_kind: string | null;
  model: string | null;
  provider: string | null;
  effort: string | null;
  status: string;
  started_at: number | null;
  ended_at: number | null;
  created_at: number;
  updated_at: number | null;
  last_message_at: number | null;
  effective_health: string | null;
  branch_count: number;
  message_count: number;
  project: string | null;
  project_source: string | null;
  invocation_id: string | null;
}

/** Paginated response from GET /api/runs/. */
export interface RunsPage {
  runs: Run[];
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

/** Body for POST /api/launches/. All fields optional except action_kind. */
export interface LaunchRequest {
  action_kind: "agent" | "flow" | "fanout" | "play" | "engine";
  action_model?: string;
  action_prompt?: string;
  action_agent?: string;
  action_playbook?: string;
  action_project?: string;
  action_flow_yaml?: string;
  action_engine_def?: string;
  action_extra_args?: string[];
}

/** Response body from POST /api/launches/ (202 Accepted). */
export interface LaunchResult {
  invocation_id: string;
  action_kind: string;
}

/** Discriminated union for SSE event objects from GET /api/sessions/{id}/stream. */
export type StudioEvent =
  | { type: "heartbeat" }
  | { type: "done" }
  | MessageEvent;

/** A generic message row event (anything other than heartbeat/done). */
export interface MessageEvent {
  type?: string;
  [key: string]: unknown;
}
