/**
 * How a run or invocation health verdict should be read.
 *
 * These live outside any one board because both boards decide the same thing
 * from them: Mission whether to present a card as live, Fleet whether a row is
 * clear. Keeping a copy per board is how the two answer the same question
 * differently after the next change to either.
 */

/**
 * Health states meaning the process is gone even though the run is
 * non-terminal. TODO(unify): route through deriveDisplayStatus once
 * status/verdict/health derivation is unified into one shared function.
 */
export const DEAD_HEALTH = new Set(["stale", "orphaned", "zombie", "unresponsive"]);

/** Whether a run's effective_health means the process is gone (never based on duration). */
export function isDeadHealth(health: string | null | undefined): boolean {
  return health != null && DEAD_HEALTH.has(health);
}

/**
 * Whether an invocation's health means liveness genuinely could not be
 * determined (e.g. no child session has landed yet) — distinct from
 * isDeadHealth, which means a process was observed and it's gone.
 */
export function isUnknownHealth(health: string | null | undefined): boolean {
  return health === "unknown";
}

/**
 * Whether an invocation's health verdict is settled enough to present.
 *
 * The verdict is worst-of across child sessions, so one read from a capped
 * sample can only err toward looking well: an unread child could be
 * unresponsive behind a sampled "idle". A verdict that already says the
 * process is gone survives truncation, because reading the rest could only
 * agree. Anything else from a partial sample is not evidence of health, and
 * it reads as unknown rather than as running.
 */
export function isUnsettledHealth(inv: {
  health?: string | null;
  health_from_partial_children?: boolean;
}): boolean {
  if (isUnknownHealth(inv.health)) return true;
  return inv.health_from_partial_children === true && !isDeadHealth(inv.health);
}
