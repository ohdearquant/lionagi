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
import { deriveDisplayStatus, isOrphanedReason } from "@/lib/runStatus";

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
  /**
   * True once a schedules fetch has succeeded. Until then the empty
   * schedules array is a placeholder, not knowledge — it must not feed
   * the systemEmpty derivation.
   */
  schedulesKnown: boolean;
  /** Items needing operator attention. */
  attentionItems: AttentionItem[];
  /**
   * True when the daemon has no work at all (no runs, invocations, or
   * schedules) — gates the zero-state guided cards. Stays false until
   * the first successful fetch so loading never flashes the cards.
   */
  systemEmpty: boolean;
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
  /** One-line failure reason — present on "failed" items when the run carries one. */
  reasonSummary?: string;
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

// Invocations (skill orchestrations) have no reason_code/reason_summary axis
// and no orphaned bucket — they keep their own lightweight classification.
// Runs go through deriveDisplayStatus() below; do not add run lifecycle
// checks against these sets.
const RUNNING_STATUSES = new Set([
  "running",
  "executing",
  "in_progress",
  "director-managed",
  "open",
]);
const FAILED_STATUSES = new Set(["failed", "error", "failure"]);
const GATED_STATUSES = new Set(["needs_review", "blocked", "gated"]);

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
    // DESIGN-BRIEF §0: a daemon-restart reap is housekeeping, never attention
    // — it must not surface here as "failed" or under any other reason.
    if (isOrphanedReason(run)) continue;
    const s = run.status.toLowerCase();
    const derived = deriveDisplayStatus(run);
    // Status-based reasons take precedence; stale health is the fallback so
    // an actionable gated/stuck run never degrades into an informational row.
    // Gating is a separate attention check, not a lifecycle status — it stays
    // a raw-string match since deriveDisplayStatus has no "gated" bucket.
    let reason: AttentionReason | null = null;
    if (derived === "failed") {
      if (!failedRecently(run.ended_at, run.started_at, nowSec)) continue;
      reason = "failed";
    } else if (GATED_STATUSES.has(s)) {
      reason = "gated";
    } else if (derived === "running" && run.effective_health === "unresponsive") {
      // Stuck is the honest health verdict (alive but quiet past its threshold),
      // never run age: a long-lived session still emitting activity is healthy.
      reason = "stuck";
    }
    if (
      reason == null &&
      (run.effective_health === "stale" ||
        run.effective_health === "orphaned" ||
        run.effective_health === "zombie")
    ) {
      reason = "stale";
    }
    if (reason == null) continue;
    items.push({
      id: `run:${run.run_id}`,
      kind: "run",
      name: run.playbook_name ?? run.agent_name ?? run.run_id.slice(-8),
      reason,
      startedAt: run.started_at ?? null,
      href: `/runs/${run.run_id}`,
      status: run.status,
      ...(reason === "failed" && run.status_reason_summary
        ? { reasonSummary: run.status_reason_summary }
        : {}),
    });
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
  return runs.filter((r) => deriveDisplayStatus(r) === "running");
}

function deriveRecentRuns(runs: RunSummary[]): RunSummary[] {
  return runs
    .filter((r) => {
      const derived = deriveDisplayStatus(r);
      return derived !== "running" && derived !== "queued";
    })
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
    schedulesKnown: false,
    attentionItems: [],
    systemEmpty: false,
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
      const schedulesKnown = state.schedulesKnown || action.schedules !== null;
      const activeRuns = deriveActiveRuns(runs);
      const activeInvocations = deriveActiveInvocations(invocations);
      const recentRuns = deriveRecentRuns(runs);
      const attentionItems = buildAttentionItems(runs, invocations, schedules, nowSec);
      // A degraded schedules fetch before the first successful one leaves an
      // empty placeholder list — never declare the system empty from it.
      const systemEmpty =
        schedulesKnown && runs.length === 0 && invocations.length === 0 && schedules.length === 0;
      return {
        ...state,
        nowSec,
        activeRuns,
        activeInvocations,
        recentRuns,
        schedules,
        schedulesKnown,
        attentionItems,
        systemEmpty,
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
