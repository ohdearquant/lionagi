/**
 * Schedules space data layer — fetch + polling + lane derivation + time math.
 * Lane membership is derived from real fields only: next_fire_at places a
 * schedule in Today or Upcoming, run status places runs in Running or Done.
 * No firing projections are invented — only the scheduler's own next_fire_at.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { listScheduleRuns, listSchedules } from "@/lib/api";
import type { ScheduleRunSummary, ScheduleSummary } from "@/lib/types";

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
export interface RunRow extends ScheduleRunSummary {
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
const RUNS_PER_SCHEDULE = 25;

/**
 * Fetches all schedules plus each schedule's recent runs (the API exposes
 * runs per schedule only), and re-polls every 30s so the Running lane is live.
 */
export function useSchedulesData(): SchedulesData {
  const [schedules, setSchedules] = useState<ScheduleSummary[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [nowMs, setNowMs] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const aliveRef = useRef(true);

  const load = useCallback(async () => {
    try {
      const res = await listSchedules();
      if (!aliveRef.current) return;
      const list = res.schedules;
      const settled = await Promise.allSettled(
        list.map((s) => listScheduleRuns(s.id, { limit: RUNS_PER_SCHEDULE })),
      );
      if (!aliveRef.current) return;
      const nameById = new Map(list.map((s) => [s.id, s.name]));
      const allRuns: RunRow[] = [];
      for (const r of settled) {
        if (r.status === "fulfilled") {
          for (const run of r.value.runs) {
            allRuns.push({
              ...run,
              scheduleName: nameById.get(run.schedule_id) ?? run.schedule_id,
            });
          }
        }
      }
      setSchedules(list);
      setRuns(allRuns);
      setNowMs(Date.now());
      setError(false);
    } catch {
      if (aliveRef.current) setError(true);
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState, but this is a data-fetch pattern matching the rest of the codebase
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => {
      aliveRef.current = false;
      clearInterval(id);
    };
  }, [load]);

  return { schedules, runs, nowMs, loading, error, refresh: () => void load() };
}

// ─── Lane derivation ──────────────────────────────────────────────────────────

export interface Lanes {
  today: ScheduleSummary[];
  upcoming: ScheduleSummary[];
  paused: ScheduleSummary[];
  running: RunRow[];
  done: RunRow[];
}

const DONE_LANE_LIMIT = 15;

export function deriveLanes(schedules: ScheduleSummary[], runs: RunRow[], nowMs: number): Lanes {
  const endOfToday = new Date(nowMs);
  endOfToday.setHours(23, 59, 59, 999);
  const eodMs = endOfToday.getTime();

  const today: ScheduleSummary[] = [];
  const upcoming: ScheduleSummary[] = [];
  const paused: ScheduleSummary[] = [];
  for (const s of schedules) {
    if (!s.enabled) {
      paused.push(s);
    } else if (s.next_fire_at != null && toMs(s.next_fire_at) <= eodMs) {
      today.push(s);
    } else {
      upcoming.push(s);
    }
  }
  const byNextFire = (a: ScheduleSummary, b: ScheduleSummary) =>
    (a.next_fire_at ?? Infinity) - (b.next_fire_at ?? Infinity);
  today.sort(byNextFire);
  upcoming.sort(byNextFire);
  paused.sort((a, b) => a.name.localeCompare(b.name));

  const running = runs
    .filter((r) => r.status === "running")
    .sort((a, b) => b.fired_at - a.fired_at);
  const done = runs
    .filter((r) => r.status !== "running")
    .sort((a, b) => (b.ended_at ?? b.fired_at) - (a.ended_at ?? a.fired_at))
    .slice(0, DONE_LANE_LIMIT);

  return { today, upcoming, paused, running, done };
}

/** Most recent run per schedule, for the "last fired" footer on cards. */
export function latestRunBySchedule(runs: RunRow[]): Map<string, RunRow> {
  const map = new Map<string, RunRow>();
  for (const r of runs) {
    const prev = map.get(r.schedule_id);
    if (!prev || r.fired_at > prev.fired_at) map.set(r.schedule_id, r);
  }
  return map;
}
