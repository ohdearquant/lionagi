/**
 * Single deep-link target for a run's detail context. Home cards and
 * attention rows all route through here so the unified Operations cutover
 * is a one-line change, not a grep.
 */
export function runDeepLink(runId: string): { to: "/fleet"; search: { s: string } } {
  return { to: "/fleet", search: { s: runId } };
}

/** Invocation counterpart of runDeepLink — same single-cutover rationale. */
export function invocationDeepLink(): { to: "/fleet" } {
  return { to: "/fleet" };
}

/** Schedule counterpart — opens the board with the schedule's detail visible. */
export function scheduleDeepLink(scheduleId: string): {
  to: "/schedules";
  search: { s: string };
} {
  return { to: "/schedules", search: { s: scheduleId } };
}

/**
 * Play counterpart. A play whose show recorded its root run/session id deep
 * links straight to that run's detail; without one (no dedicated show/play
 * detail route exists — the legacy `/shows` route redirects to `/fleet`)
 * this stays the same best-effort landing spot as `invocationDeepLink`.
 */
export function playDeepLink(sessionId?: string | null): { to: "/fleet"; search?: { s: string } } {
  return sessionId ? { to: "/fleet", search: { s: sessionId } } : { to: "/fleet" };
}
