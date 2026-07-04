// ADR-0093: the single status oracle. Every projection, attention chip, and
// slide-over calls this same function so "running" never means something
// different on two surfaces. It never emits or accepts the word
// "invocation" — that noun stays internal plumbing.

export type RunSource = "agent" | "schedule" | "script" | "flow";

export type DerivedStatus =
  | "running"
  | "completed"
  | "failed"
  | "stale"
  | "expired"
  | "cancelled"
  | "pending";

export interface DerivedRunStatus {
  status: DerivedStatus;
  /** True when a running (or just-completed) run has taken unusually long. */
  isSlow: boolean;
  durationSeconds: number | null;
}

export interface RunLivenessInput {
  source: RunSource;
  /** Raw backend status string for this source's own vocabulary. */
  rawStatus: string;
  startedAt: number | null;
  endedAt: number | null;
  /** Epoch seconds, injected so this stays a pure, testable function. */
  now: number;
  /**
   * Real process liveness for source="agent" only, cross-referenced from
   * `/api/admin/health`'s `sessions.unhealthy[]` (a `ps`/pid-file scan).
   * `RunSummary.effective_health` alone can't be trusted here: it hardcodes
   * `process_alive=true` and can only ever detect idle-timeout staleness,
   * never a genuinely dead process. Undefined/true = trust the raw status;
   * false = the process is confirmed dead, demote "running" to "stale".
   */
  processAlive?: boolean;
  /**
   * Schedule-source only: this run belongs to a schedule that has already
   * fired and will never fire again (disabled, no next_fire_at). A
   * completed run under a spent schedule renders "expired" rather than
   * "completed" — it's not just done, it's retired.
   */
  isSpentOneShotSchedule?: boolean;
}

const RUNNING_RAW = new Set(["running", "executing", "in_progress", "director-managed", "open"]);
const PENDING_RAW = new Set([
  "pending",
  "queued",
  "prepared",
  "planned",
  "received",
  "waiting",
  "scheduled",
  "gated",
  "blocked",
  "needs_review",
]);
const FAILED_RAW = new Set(["failed", "error", "failure"]);
const CANCELLED_RAW = new Set(["cancelled", "canceled", "aborted", "skipped"]);

// Matches the dashboard's existing SLOW_RUN_SECONDS/STUCK_RUN_SECONDS
// precedent (typical runs are 45-90 min; 30 min would flag nearly every
// flow). Sources without a real liveness signal (schedule/script/flow) use
// the same bound as a no-heartbeat staleness heuristic, since they have no
// admin/health equivalent to cross-reference against.
const SLOW_AFTER_SECONDS = 60 * 60;
const STALE_AFTER_SECONDS = 60 * 60;

export function deriveRunStatus(input: RunLivenessInput): DerivedRunStatus {
  const { source, startedAt, endedAt, now, processAlive, isSpentOneShotSchedule } = input;
  const key = input.rawStatus.toLowerCase().trim();
  const durationSeconds = startedAt == null ? null : (endedAt ?? now) - startedAt;

  if (RUNNING_RAW.has(key)) {
    if (source === "agent" && processAlive === false) {
      return { status: "stale", isSlow: false, durationSeconds };
    }
    if (source !== "agent" && durationSeconds != null && durationSeconds > STALE_AFTER_SECONDS) {
      return { status: "stale", isSlow: false, durationSeconds };
    }
    const isSlow = durationSeconds != null && durationSeconds > SLOW_AFTER_SECONDS;
    return { status: "running", isSlow, durationSeconds };
  }

  if (PENDING_RAW.has(key)) {
    return { status: "pending", isSlow: false, durationSeconds };
  }
  if (FAILED_RAW.has(key)) {
    return { status: "failed", isSlow: false, durationSeconds };
  }
  if (CANCELLED_RAW.has(key)) {
    return { status: "cancelled", isSlow: false, durationSeconds };
  }

  // Everything else is a terminal "done" state (completed/done/success/
  // finished/running_complete/... and any status string we don't
  // recognize yet) — unless it belongs to a schedule that's now spent.
  if (source === "schedule" && isSpentOneShotSchedule) {
    return { status: "expired", isSlow: false, durationSeconds };
  }
  const isSlow = durationSeconds != null && durationSeconds > SLOW_AFTER_SECONDS;
  return { status: "completed", isSlow, durationSeconds };
}
