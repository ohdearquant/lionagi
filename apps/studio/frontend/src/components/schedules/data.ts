/**
 * Schedules space data layer — fetch + polling + time math. One row per
 * schedule; run history lives on the schedule detail page, not here.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { listScheduleSummary } from "@/lib/api";
import type { ScheduleRunSliceRow, ScheduleSummary } from "@/lib/types";

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
  /** Schedule ids whose recent-run slice is missing or failed. */
  runSummaryErrors: ReadonlySet<string>;
  refresh: () => void;
}

const POLL_MS = 30_000;
const RUNS_PER_SCHEDULE = 25;

/**
 * Fetches schedules and their bounded recent-run slices in one request, and
 * re-polls every 30s so the Running lane is live.
 */
export function useSchedulesData(): SchedulesData {
  const [schedules, setSchedules] = useState<ScheduleSummary[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [nowMs, setNowMs] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [runSummaryErrors, setRunSummaryErrors] = useState<ReadonlySet<string>>(new Set());
  const aliveRef = useRef(true);
  const inFlightRef = useRef<Promise<void> | null>(null);
  const trailingRefreshRef = useRef(false);

  const load = useCallback(function loadSchedules(): Promise<void> {
    if (inFlightRef.current) {
      trailingRefreshRef.current = true;
      return inFlightRef.current;
    }

    const request = (async () => {
      try {
        const res = await listScheduleSummary({ recentRunsLimit: RUNS_PER_SCHEDULE });
        if (!aliveRef.current) return;
        const list = res.schedules;
        const nameById = new Map(list.map((s) => [s.id, s.name]));
        const allRuns: RunRow[] = [];
        const summaryErrors = new Set<string>();
        for (const schedule of list) {
          const summary = res.run_summaries[schedule.id];
          if (!summary || summary.state !== "ok") {
            summaryErrors.add(schedule.id);
            continue;
          }
          for (const run of summary.runs) {
            allRuns.push({
              ...run,
              scheduleName: nameById.get(run.schedule_id) ?? run.schedule_id,
            });
          }
        }
        setSchedules(list);
        setRuns(allRuns);
        setRunSummaryErrors(summaryErrors);
        setNowMs(Date.now());
        setError(summaryErrors.size > 0);
      } catch {
        if (aliveRef.current) setError(true);
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    })();
    inFlightRef.current = request;
    void request.finally(() => {
      if (inFlightRef.current !== request) return;
      inFlightRef.current = null;
      if (trailingRefreshRef.current && aliveRef.current) {
        trailingRefreshRef.current = false;
        void loadSchedules();
      }
    });
    return request;
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => {
      aliveRef.current = false;
      trailingRefreshRef.current = false;
      clearInterval(id);
    };
  }, [load]);

  return {
    schedules,
    runs,
    nowMs,
    loading,
    error,
    runSummaryErrors,
    refresh: () => void load(),
  };
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
