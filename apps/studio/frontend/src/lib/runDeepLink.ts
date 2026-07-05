/**
 * Single deep-link target for a run's detail context. Home cards and
 * attention rows all route through here so the unified Operations cutover
 * is a one-line change, not a grep.
 */
export function runDeepLink(runId: string): { to: "/fleet"; search: { s: string } } {
  return { to: "/fleet", search: { s: runId } };
}

/** Invocation counterpart of runDeepLink — same single-cutover rationale. */
export function invocationDeepLink(): { to: "/history"; search: { tab: "run" } } {
  return { to: "/history", search: { tab: "run" } };
}

/** Schedule counterpart — opens the board with the schedule's detail visible. */
export function scheduleDeepLink(scheduleId: string): {
  to: "/schedules";
  search: { s: string };
} {
  return { to: "/schedules", search: { s: scheduleId } };
}
