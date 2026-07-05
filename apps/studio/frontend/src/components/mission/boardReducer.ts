/**
 * Mission Control board reducer.
 *
 * All live-update sources (polling or future SSE) funnel through
 * dispatch(). Components never mutate state directly. Swapping the
 * polling loop for an SSE subscription only touches the data-source
 * layer — reducer and components are unchanged.
 */

import type { RunSummary, ScheduleSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";

// ─── State shape ─────────────────────────────────────────────────────────────

export type DataState = "loading" | "live" | "stale" | "error";

export interface BoardState {
  /** Wall-clock seconds, updated every second client-side. */
  nowSec: number;
  /** Active runs — feeds live board cards. */
  activeRuns: RunSummary[];
  /** Active invocations (skill orchestrations). */
  activeInvocations: InvocationSummary[];
  /** Last 10 terminal runs (completed/failed/cancelled). */
  recentRuns: RunSummary[];
  /** Enabled schedules — feeds failure-streak attention rows. */
  schedules: ScheduleSummary[];
  /** Items needing operator attention. */
  attentionItems: AttentionItem[];
  /** Data freshness state (3 distinct states + loading). */
  dataState: DataState;
  /** Epoch ms of the last successful data update. */
  lastUpdatedMs: number | null;
  /** Error message when dataState === "error". */
  errorMessage: string | null;
}

export type AttentionReason = "streak" | "failed" | "stale" | "stuck" | "gated";

export interface AttentionItem {
  id: string;
  kind: "run" | "invocation" | "schedule";
  name: string;
  reason: AttentionReason;
  startedAt: number | null;
  href: string;
  status: string;
  /** Consecutive-failure count — present on "streak" items only. */
  streakCount?: number;
}

// ─── Actions ─────────────────────────────────────────────────────────────────

export type BoardAction =
  | { type: "TICK"; nowSec: number }
  | {
      type: "DATA_OK";
      runs: RunSummary[];
      invocations: InvocationSummary[];
      /** null = schedules fetch failed this cycle — keep the last-known list. */
      schedules: ScheduleSummary[] | null;
      nowSec: number;
    }
  | { type: "DATA_ERROR"; message: string }
  | { type: "MARK_STALE" };

// ─── Status classification constants ─────────────────────────────────────────

const RUNNING_STATUSES = new Set([
  "running",
  "executing",
  "in_progress",
  "director-managed",
  "open",
]);
const FAILED_STATUSES = new Set(["failed", "error", "failure"]);
const TERMINAL_STATUSES = new Set([
  "completed",
  "done",
  "success",
  "finished",
  "running_complete",
  "director-managed-complete",
  "failed",
  "error",
  "failure",
  "cancelled",
  "aborted",
  "timed_out",
]);
const GATED_STATUSES = new Set(["needs_review", "blocked", "gated"]);

/** Runs stalled longer than this (seconds) are flagged "stuck". */
const STUCK_THRESHOLD_SEC = 60 * 60;

/** Failures older than this belong to History, not the attention queue. */
const FAILED_ATTENTION_WINDOW_SEC = 24 * 60 * 60;

/** Schedules failing this many consecutive runs get an attention row. */
export const STREAK_ATTENTION_THRESHOLD = 3;

function failedRecently(
  endedAt: number | null | undefined,
  startedAt: number | null | undefined,
  nowSec: number,
): boolean {
  const ref = endedAt ?? startedAt;
  if (ref == null) return false;
  return nowSec - ref <= FAILED_ATTENTION_WINDOW_SEC;
}

// ─── Derivation helpers ───────────────────────────────────────────────────────

function elapsedSec(startedAt: number | null, nowSec: number): number | null {
  if (startedAt == null) return null;
  return nowSec - startedAt;
}

function buildAttentionItems(
  runs: RunSummary[],
  invocations: InvocationSummary[],
  schedules: ScheduleSummary[],
  nowSec: number,
): AttentionItem[] {
  const items: AttentionItem[] = [];

  for (const sched of schedules) {
    if (!sched.enabled) continue;
    const streak = sched.consecutive_failures ?? 0;
    if (streak < STREAK_ATTENTION_THRESHOLD) continue;
    items.push({
      id: `sched:${sched.id}`,
      kind: "schedule",
      name: sched.name,
      reason: "streak",
      startedAt: sched.last_fired_at ?? null,
      href: "/schedules",
      status: sched.last_status ?? "failed",
      streakCount: streak,
    });
  }

  for (const run of runs) {
    const s = run.status.toLowerCase();
    if (FAILED_STATUSES.has(s)) {
      if (!failedRecently(run.ended_at, run.started_at, nowSec)) continue;
      items.push({
        id: `run:${run.run_id}`,
        kind: "run",
        name: run.playbook_name ?? run.agent_name ?? run.run_id.slice(-8),
        reason: "failed",
        startedAt: run.started_at ?? null,
        href: `/runs/${run.run_id}`,
        status: run.status,
      });
    } else if (run.effective_health === "stale" || run.effective_health === "orphaned") {
      items.push({
        id: `run:${run.run_id}`,
        kind: "run",
        name: run.playbook_name ?? run.agent_name ?? run.run_id.slice(-8),
        reason: "stale",
        startedAt: run.started_at ?? null,
        href: `/runs/${run.run_id}`,
        status: run.status,
      });
    } else if (RUNNING_STATUSES.has(s)) {
      const elapsed = elapsedSec(run.started_at ?? null, nowSec);
      if (elapsed != null && elapsed > STUCK_THRESHOLD_SEC) {
        items.push({
          id: `run:${run.run_id}`,
          kind: "run",
          name: run.playbook_name ?? run.agent_name ?? run.run_id.slice(-8),
          reason: "stuck",
          startedAt: run.started_at ?? null,
          href: `/runs/${run.run_id}`,
          status: run.status,
        });
      }
    } else if (GATED_STATUSES.has(s)) {
      items.push({
        id: `run:${run.run_id}`,
        kind: "run",
        name: run.playbook_name ?? run.agent_name ?? run.run_id.slice(-8),
        reason: "gated",
        startedAt: run.started_at ?? null,
        href: `/runs/${run.run_id}`,
        status: run.status,
      });
    }
  }

  for (const inv of invocations) {
    const s = inv.status.toLowerCase();
    if (FAILED_STATUSES.has(s)) {
      if (!failedRecently(inv.ended_at, inv.started_at, nowSec)) continue;
      items.push({
        id: `inv:${inv.id}`,
        kind: "invocation",
        name: inv.skill,
        reason: "failed",
        startedAt: inv.started_at ?? null,
        href: `/invocations/${inv.id}`,
        status: inv.status,
      });
    } else if (GATED_STATUSES.has(s)) {
      items.push({
        id: `inv:${inv.id}`,
        kind: "invocation",
        name: inv.skill,
        reason: "gated",
        startedAt: inv.started_at ?? null,
        href: `/invocations/${inv.id}`,
        status: inv.status,
      });
    }
  }

  // Sort: streak first, then gated, stuck, failed, stale; within group by recency
  const ORDER: Record<AttentionReason, number> = {
    streak: 0,
    gated: 1,
    stuck: 2,
    failed: 3,
    stale: 4,
  };
  items.sort((a, b) => {
    const od = ORDER[a.reason] - ORDER[b.reason];
    if (od !== 0) return od;
    return (b.startedAt ?? 0) - (a.startedAt ?? 0);
  });

  // Deduplicate by id (a run could match multiple reasons — take first/worst)
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function deriveActiveRuns(runs: RunSummary[]): RunSummary[] {
  return runs.filter((r) => RUNNING_STATUSES.has(r.status.toLowerCase()));
}

function deriveRecentRuns(runs: RunSummary[]): RunSummary[] {
  return runs
    .filter((r) => TERMINAL_STATUSES.has(r.status.toLowerCase()))
    .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
    .slice(0, 10);
}

function deriveActiveInvocations(invocations: InvocationSummary[]): InvocationSummary[] {
  return invocations.filter((i) => RUNNING_STATUSES.has(i.status.toLowerCase()));
}

// ─── Initial state ────────────────────────────────────────────────────────────

export function initialBoardState(): BoardState {
  return {
    nowSec: Math.floor(Date.now() / 1000),
    activeRuns: [],
    activeInvocations: [],
    recentRuns: [],
    schedules: [],
    attentionItems: [],
    dataState: "loading",
    lastUpdatedMs: null,
    errorMessage: null,
  };
}

// ─── Reducer ──────────────────────────────────────────────────────────────────

export function boardReducer(state: BoardState, action: BoardAction): BoardState {
  switch (action.type) {
    case "TICK":
      return { ...state, nowSec: action.nowSec };

    case "DATA_OK": {
      const { runs, invocations, nowSec } = action;
      const schedules = action.schedules ?? state.schedules;
      const activeRuns = deriveActiveRuns(runs);
      const activeInvocations = deriveActiveInvocations(invocations);
      const recentRuns = deriveRecentRuns(runs);
      const attentionItems = buildAttentionItems(runs, invocations, schedules, nowSec);
      return {
        ...state,
        nowSec,
        activeRuns,
        activeInvocations,
        recentRuns,
        schedules,
        attentionItems,
        dataState: "live",
        lastUpdatedMs: Date.now(),
        errorMessage: null,
      };
    }

    case "DATA_ERROR":
      return {
        ...state,
        dataState: "error",
        errorMessage: action.message,
      };

    case "MARK_STALE":
      // Only transition from live → stale; don't clobber error state.
      if (state.dataState === "live") {
        return { ...state, dataState: "stale" };
      }
      return state;

    default:
      return state;
  }
}
