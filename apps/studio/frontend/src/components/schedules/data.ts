/**
 * Schedules space data layer — fetch + polling + time math. One row per
 * schedule, carrying its newest-25 run slice from the list response; the full
 * paginated history stays on the schedule detail page.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { listScheduleRuns, listSchedules } from "@/lib/api";
import type { ScheduleRecentRunsSummary, ScheduleRunSliceRow, ScheduleSummary } from "@/lib/types";

// Statuses with a history.status translation; unknown values fall back to
// StatusPill's built-in humanization.
export const KNOWN_RUN_STATUSES = new Set([
  "running",
  "completed",
  "failed",
  "cancelled",
  "pending",
  "queued",
  "timed_out",
  "aborted",
  "skipped",
]);

/** Epoch values arrive in seconds or ms depending on producer — normalize to ms. */
export function toMs(value: number): number {
  return value > 1e12 ? value : value * 1000;
}

/** Compact duration: "42s", "14m", "2h 4m", "3d 7h". Clamps negatives to 0. */
export function formatDelta(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) {
    const rm = m % 60;
    return rm ? `${h}h ${rm}m` : `${h}h`;
  }
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}

/** "90s" → "1m 30s", "3600" → "1h" — for interval_sec / poll_interval_sec. */
export function formatInterval(sec: number): string {
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return s ? `${m}m ${s}s` : `${m}m`;
  }
  if (sec < 86400) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return h ? `${d}d ${h}h` : `${d}d`;
}

/** A run joined with its parent schedule's name for display. */
export interface RunRow extends ScheduleRunSliceRow {
  scheduleName: string;
}

export interface SchedulesData {
  schedules: ScheduleSummary[];
  runs: RunRow[];
  /** Wall-clock at last successful fetch — countdowns derive from this, not render time. */
  nowMs: number;
  loading: boolean;
  error: boolean;
  refresh: () => void;
}

const POLL_MS = 30_000;

function recentSummary(schedule: ScheduleSummary): ScheduleRecentRunsSummary {
  const summary = schedule.recent_runs_summary;
  if (
    summary?.version === 1 &&
    summary.limit === 25 &&
    summary.status === "available" &&
    typeof summary.has_next === "boolean" &&
    Array.isArray(summary.runs) &&
    summary.runs.length <= 25 &&
    (!summary.has_next || summary.runs.length === 25) &&
    summary.runs.every((run) => run && run.schedule_id === schedule.id)
  ) {
    return summary;
  }
  return { version: 1, limit: 25, status: "unavailable", has_next: null, runs: [] };
}

export function recentRunsUnavailable(schedule: ScheduleSummary): boolean {
  return recentSummary(schedule).status === "unavailable";
}

/**
 * Read one schedule's history the way a daemon that predates the summary field
 * serves it, and shape it as the same slice. A failed read stays unavailable,
 * so a short history is never reported as empty.
 */
async function historySummary(schedule: ScheduleSummary): Promise<ScheduleRecentRunsSummary> {
  try {
    const { runs, has_next } = await listScheduleRuns(schedule.id, { limit: 25 });
    return { version: 1, limit: 25, status: "available", has_next, runs };
  } catch {
    return { version: 1, limit: 25, status: "unavailable", has_next: null, runs: [] };
  }
}

/**
 * One list request includes recent run slices. Overlapping polls and explicit
 * refreshes share the in-flight request and queue at most one trailing refresh.
 */
export function useSchedulesData(): SchedulesData {
  const [schedules, setSchedules] = useState<ScheduleSummary[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [nowMs, setNowMs] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const refreshRef = useRef<() => void>(() => {});
  const refresh = useCallback(() => refreshRef.current(), []);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    let pending = false;
    const load = async () => {
      if (!active) return;
      if (inFlight) {
        pending = true;
        return;
      }
      inFlight = true;
      try {
        const res = await listSchedules({ include_recent_runs: true });
        if (!active) return;
        // A daemon that predates the summary field omits it; that row is read
        // through its own history endpoint rather than shown unavailable.
        const list = await Promise.all(
          res.schedules.map(async (schedule) => ({
            ...schedule,
            recent_runs_summary:
              schedule.recent_runs_summary === undefined
                ? await historySummary(schedule)
                : recentSummary(schedule),
          })),
        );
        if (!active) return;
        setSchedules(list);
        setRuns(
          list.flatMap((schedule) =>
            schedule.recent_runs_summary.runs.map((run) => ({
              ...run,
              scheduleName: schedule.name,
            })),
          ),
        );
        setNowMs(Date.now());
        setError(false);
      } catch {
        if (active) setError(true);
      } finally {
        inFlight = false;
        if (active) {
          setLoading(false);
          if (pending) {
            pending = false;
            void load();
          }
        }
      }
    };
    refreshRef.current = () => void load();
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  return { schedules, runs, nowMs, loading, error, refresh };
}

// ─── Next-fire derivation ─────────────────────────────────────────────────────
// A disabled schedule never fires, so its stored next_fire_at is stale and must
// never be shown as an upcoming (or overdue) firing. Enabled state is checked
// FIRST here — every surface that shows "next fire" derives it through this so
// they can't disagree.

const SOON_MS = 3_600_000; // within the hour

export type NextFireState =
  | { kind: "paused" }
  | { kind: "watching" }
  | { kind: "unscheduled" }
  | { kind: "fire"; fireMs: number; deltaMs: number; overdue: boolean; soon: boolean };

export function nextFireState(s: ScheduleSummary, nowMs: number): NextFireState {
  if (!s.enabled) return { kind: "paused" };
  if (s.trigger_type === "github_poll") return { kind: "watching" };
  if (s.next_fire_at == null) return { kind: "unscheduled" };
  const fireMs = toMs(s.next_fire_at);
  const deltaMs = fireMs - nowMs;
  return {
    kind: "fire",
    fireMs,
    deltaMs,
    overdue: deltaMs < 0,
    soon: deltaMs >= 0 && deltaMs < SOON_MS,
  };
}

/**
 * Card ordering: live schedules first (soonest firing at the top), paused ones
 * sink to the bottom — the reverse of showing a disabled schedule as "overdue".
 */
export function sortSchedulesForCards(schedules: ScheduleSummary[]): ScheduleSummary[] {
  return [...schedules].sort((a, b) => {
    const ea = a.enabled ? 0 : 1;
    const eb = b.enabled ? 0 : 1;
    if (ea !== eb) return ea - eb;
    if (a.next_fire_at == null && b.next_fire_at == null) return a.name.localeCompare(b.name);
    if (a.next_fire_at == null) return 1;
    if (b.next_fire_at == null) return -1;
    return a.next_fire_at - b.next_fire_at;
  });
}

// ─── Health badge derivation ───────────────────────────────────────────────
// The backend computes health from cadence + recorded schedule_runs rows, so
// this is a pure display mapping only -- it never re-derives the verdict, it
// just turns the server's fields into what a badge needs to render.

export type HealthBadgeState =
  | { kind: "hidden" }
  | { kind: "healthy" | "failing" | "overdue"; outcome: string | null; outcomeAtMs: number | null }
  | { kind: "never-fired"; sinceMs: number }
  | { kind: "no-evidence" };

// The server-declared health_state union is a compile-time contract only --
// a running server can still send a value this build predates or has never
// heard of (mid-rollout skew, a state added and rolled back). Record lookups
// keyed by the declared union (HEALTH_COLOR, HEALTH_LABEL_KEY) crash on
// anything outside it, so unrecognized values must be caught here, before
// they reach a badge, and fall back to hidden -- the same neutral no-badge
// treatment "disabled" already gets.
const KNOWN_HEALTH_STATES = new Set([
  "healthy",
  "failing",
  "overdue",
  "never-fired",
  "no-evidence",
  "disabled",
]);

export function scheduleHealthBadge(s: ScheduleSummary): HealthBadgeState {
  const state = s.health_state;
  if (!state || !KNOWN_HEALTH_STATES.has(state) || state === "disabled") return { kind: "hidden" };
  if (state === "never-fired") {
    return { kind: "never-fired", sinceMs: toMs(s.health_since ?? 0) };
  }
  if (state === "no-evidence") {
    return { kind: "no-evidence" };
  }
  return {
    kind: state,
    outcome: s.health_last_outcome ?? null,
    outcomeAtMs: s.health_last_outcome_at != null ? toMs(s.health_last_outcome_at) : null,
  };
}

/** Most recent run per schedule, for the table's "last run" column. */
export function latestRunBySchedule(runs: RunRow[]): Map<string, RunRow> {
  const map = new Map<string, RunRow>();
  for (const r of runs) {
    const prev = map.get(r.schedule_id);
    if (!prev || r.fired_at > prev.fired_at) map.set(r.schedule_id, r);
  }
  return map;
}
