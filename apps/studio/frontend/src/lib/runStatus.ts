/**
 * DESIGN-BRIEF §0: status and verdict are two independent axes. This module
 * is the ONE derivation function both the list/overview side (boardReducer,
 * RecentRuns) and the detail side (RunDetail) must call — no site re-derives
 * status on its own.
 *
 * run_status (infra truth): queued | running | completed | failed | cancelled | orphaned
 * verdict (outcome of a completed run, only ever from a structured emission —
 * never scraped from message text): approve | approve-with-fixes |
 * request-changes | reject | none
 */

export type DisplayStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "orphaned";

export type Verdict = "approve" | "approve-with-fixes" | "request-changes" | "reject" | "none";

export interface RunStatusInput {
  status: string;
  status_reason_code?: string | null;
  status_reason_summary?: string | null;
  effective_health?: "healthy" | "idle" | "unresponsive" | "stale" | "orphaned" | "zombie" | null;
}

const RUNNING_ALIASES = new Set(["running", "executing", "in_progress", "director-managed"]);
const GATED_ALIASES = new Set(["needs_review", "blocked", "gated"]);
const COMPLETED_ALIASES = new Set([
  "completed",
  "done",
  "success",
  "finished",
  "running_complete",
  "director-managed-complete",
]);
const FAILED_ALIASES = new Set(["failed", "error", "failure"]);
const CANCELLED_ALIASES = new Set(["cancelled", "canceled", "aborted", "timed_out", "timeout"]);
const QUEUED_ALIASES = new Set([
  "queued",
  "pending",
  "planned",
  "received",
  "waiting",
  "scheduled",
]);

// ADR-0028: the automatic phantom reaper (lifecycle.py reap_phantom_sessions)
// always stamps this literal reason_summary, regardless of which phantom
// sub-reason it detected. Zombie (stale-locks) is deliberately excluded: a
// resource leak is a real problem, not housekeeping, and stays red/failed.
const PHANTOM_REAPED_SUMMARY = "phantom_reaped";
const ZOMBIE_REASON_CODE = "session.zombie.stale_locks";

/** True when the run's terminal "failed" is actually a daemon-restart reap, not a real failure. */
export function isOrphanedReason(run: RunStatusInput): boolean {
  if (run.status_reason_code === ZOMBIE_REASON_CODE) return false;
  return run.status_reason_summary === PHANTOM_REAPED_SUMMARY;
}

/**
 * The single source of truth for "what status chip does this run show."
 * Both list/overview rows and the run detail header must call this — never
 * re-derive from `run.status` directly, or the two will drift (the exact bug
 * this function exists to close: a phantom-reaped run reading FAILED in one
 * place and completed in another).
 */
export function deriveDisplayStatus(run: RunStatusInput): DisplayStatus {
  if (isOrphanedReason(run)) return "orphaned";
  const s = (run.status || "").toLowerCase().trim();
  if (FAILED_ALIASES.has(s)) return "failed";
  if (CANCELLED_ALIASES.has(s)) return "cancelled";
  if (COMPLETED_ALIASES.has(s)) return "completed";
  if (RUNNING_ALIASES.has(s) || GATED_ALIASES.has(s)) return "running";
  if (QUEUED_ALIASES.has(s)) return "queued";
  // Unrecognized status: default toward "still going" rather than mislabeling
  // an unknown value as done or failed.
  return "running";
}

const DEAD_EFFECTIVE_HEALTH = new Set(["stale", "orphaned", "zombie"]);

/**
 * True when a run is both display-active (running/queued) AND, for running
 * rows, not already confirmed dead by the shared health classifier. A row
 * whose process is a stale/orphaned/zombie process is a Fleet/Mission bug
 * lying in wait if isActive() only ever checks display status. Kept separate
 * from deriveDisplayStatus() so mapping dead health here never changes the
 * status chip a run renders elsewhere (e.g. Mission attention branching).
 */
export function isEffectivelyActive(run: RunStatusInput): boolean {
  const derived = deriveDisplayStatus(run);
  if (derived !== "running" && derived !== "queued") return false;
  return !(
    derived === "running" &&
    run.effective_health != null &&
    DEAD_EFFECTIVE_HEALTH.has(run.effective_health)
  );
}

const VERDICT_ALIASES: Record<string, Verdict> = {
  approve: "approve",
  approved: "approve",
  pass: "approve",
  "approve-with-fixes": "approve-with-fixes",
  approve_with_fixes: "approve-with-fixes",
  "approve-with-suggestions": "approve-with-fixes",
  approve_with_suggestions: "approve-with-fixes",
  "request-changes": "request-changes",
  request_changes: "request-changes",
  reject: "reject",
  rejected: "reject",
  fail: "reject",
};

/**
 * Normalizes a verdict value from a structured emission (e.g. a
 * `review_verdict` artifact's `content.verdict` field) into the closed
 * Verdict union. This is the ONLY legitimate input shape — never pass
 * free-text/transcript content through this. Unrecognized or missing input
 * renders no verdict chip at all; it never guesses.
 */
export function deriveVerdict(rawVerdict: string | null | undefined): Verdict {
  if (!rawVerdict) return "none";
  const key = rawVerdict.toLowerCase().trim();
  return VERDICT_ALIASES[key] ?? "none";
}
