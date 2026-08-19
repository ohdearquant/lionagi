/**
 * Mission Control board reducer.
 *
 * All live-update sources (polling or future SSE) funnel through
 * dispatch(). Components never mutate state directly. Swapping the
 * polling loop for an SSE subscription only touches the data-source
 * layer — reducer and components are unchanged.
 */

import type { RunSummary, ScheduleSummary } from "@/lib/types";
import type { AttentionDisposition, GatedPlaySummary, InvocationSummary } from "@/lib/api";
import { deriveDisplayStatus, isOrphanedReason } from "@/lib/runStatus";
import { resolveRunLabel } from "@/lib/runLabel";
import { legacyRunId, runSessionId } from "@/lib/runIdentity";

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
  /** Server-persisted discharge dispositions, keyed by attention item id. */
  dispositions: Record<string, AttentionDisposition>;
  /**
   * Plays currently in the `gated` lifecycle status, read live from the
   * shows/plays backend — the only real source of a production gate.
   * Sessions and invocations have no `gated` status in their vocabulary.
   */
  gatedPlays: GatedPlaySummary[];
  /** Items needing operator attention — open + acknowledged (visible by default). */
  attentionItems: AttentionItem[];
  /**
   * Items discharged as resolved/expected/snoozed — excluded from the
   * default view, shown only when an operator asks to see them.
   */
  dischargedAttentionItems: AttentionItem[];
  /** attentionItems with no disposition at all (excludes acknowledged). */
  unacknowledgedAttentionCount: number;
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
  kind: "run" | "invocation" | "schedule" | "play";
  name: string;
  reason: AttentionReason;
  startedAt: number | null;
  href: string;
  status: string;
  /** Consecutive-failure count — present on "streak" items only. */
  streakCount?: number;
  /**
   * One-line context — the failure reason on "failed" items when the run
   * carries one, or the gate feedback on "gated" play items.
   */
  reasonSummary?: string;
  /** Server-persisted discharge state, joined by id. Absent = "open". */
  disposition?: AttentionDisposition;
  /**
   * The play root's run/session id — present on "play" items whose show
   * recorded one, so the row can deep-link to the actual run instead of
   * landing on the bare fleet list.
   */
  sessionId?: string | null;
  /**
   * The id this item was keyed on before routing moved to the session `id`.
   * Only set when the two differ; read when joining persisted dispositions so
   * an item discharged under the old key stays discharged.
   */
  legacyId?: string;
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
      /** null = dispositions fetch failed this cycle — keep the last-known map. */
      dispositions?: Record<string, AttentionDisposition> | null;
      /** null = gated-plays fetch failed this cycle — keep the last-known list. */
      gatedPlays?: GatedPlaySummary[] | null;
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
// "blocked" deliberately excluded: it is a terminal play status (a dead-end,
// e.g. an invalid dependency), never an awaiting-approval one — treating it
// as gated would put a run that finished this way perpetually "waiting" for
// a decision nobody can make. The real gated signal for plays is sourced
// live in buildAttentionItems() below, not inferred from a status string
// here — sessions/invocations have no "gated" status in their own
// vocabulary; these two aliases exist only for a legacy/synthetic status
// string that predates that split.
const GATED_STATUSES = new Set(["needs_review", "gated"]);

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

/** Disposition states that discharge an item out of the default active view. */
const DISCHARGED_STATES: ReadonlySet<AttentionDisposition["state"]> = new Set([
  "resolved",
  "expected",
  "snoozed",
]);

function buildAttentionItems(
  runs: RunSummary[],
  invocations: InvocationSummary[],
  schedules: ScheduleSummary[],
  nowSec: number,
  dispositions: Record<string, AttentionDisposition>,
  gatedPlays: GatedPlaySummary[],
): { active: AttentionItem[]; discharged: AttentionItem[] } {
  const items: AttentionItem[] = [];

  // Plays are the only entity whose lifecycle actually contains "gated" — a
  // human-actionable state waiting on a real decision, not inferred from a
  // run/invocation status string that can never carry it.
  for (const play of gatedPlays) {
    items.push({
      // Already prefixed like every other disposition key: the gated-plays
      // feed's id is `play:<topic>:<name>` (services/shows.py), stable
      // across polls.
      id: play.id,
      kind: "play",
      name: `${play.topic} / ${play.play_name}`,
      reason: "gated",
      startedAt: play.started_at,
      href: play.session_id ? `/fleet?s=${play.session_id}` : "/fleet",
      status: play.status ?? "gated",
      sessionId: play.session_id,
      ...(play.feedback ? { reasonSummary: play.feedback } : {}),
    });
  }

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

  // A legacy key only stands in for a row when it names exactly one. Today the
  // runs projection reports run_id as the session id, so no row has a legacy
  // key at all; once those are distinct values, nothing guarantees run_id is
  // unique across sessions, and a shared one would join two rows to a single
  // stored disposition — one row's undo would then clear the other's.
  //
  // Both keys a row can present are counted, not just its legacy one: a row
  // whose id and run_id are the same value has no legacy key, yet it still
  // occupies that value as its current key, and a second session naming it as
  // run_id would otherwise claim it as unshared.
  const keyCounts = new Map<string, number>();
  const countKey = (key: string | null) => {
    if (key) keyCounts.set(key, (keyCounts.get(key) ?? 0) + 1);
  };
  for (const run of runs) {
    countKey(runSessionId(run) || null);
    countKey(legacyRunId(run));
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
    const legacy = legacyRunId(run);
    const runLegacyId = legacy && keyCounts.get(legacy) === 1 ? legacy : null;
    items.push({
      id: `run:${runSessionId(run)}`,
      ...(runLegacyId ? { legacyId: `run:${runLegacyId}` } : {}),
      kind: "run",
      name: resolveRunLabel(run),
      reason,
      startedAt: run.started_at ?? null,
      href: `/runs/${runSessionId(run)}`,
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
  const deduped = items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });

  // Join the persisted disposition (if any) onto each derived item, then
  // split into the default-visible active list and the discharged list.
  // "open" (no disposition) and "acknowledged" stay in the active list —
  // acknowledged must stay visible, only restyled; resolved/expected/snoozed
  // leave it but remain queryable via dischargedAttentionItems. A lapsed
  // snoozed/expected disposition is never present here at all: the server
  // read already drops it, so the item is simply "open" again.
  //
  // A gated item is the one exception: resolved/expected/snoozed are UI
  // states the old (pre-gate-aware) discharge controls could write, and the
  // item id is stable (`run:<id>`/`inv:<id>`/`play:<topic>:<name>`), so that
  // stale disposition is reachable forever — a genuine gate would silently
  // vanish from the default queue with no path back except editing the
  // store directly. A gated item only ever discharges via "acknowledged".
  const active: AttentionItem[] = [];
  const discharged: AttentionItem[] = [];
  for (const item of deduped) {
    const disposition =
      dispositions[item.id] ?? (item.legacyId ? dispositions[item.legacyId] : undefined);
    const joined = disposition ? { ...item, disposition } : item;
    // "A gated item only ever discharges via acknowledged" (see the comment
    // above) — this is that arm. Without it, acknowledged isn't in
    // DISCHARGED_STATES and the reason guard blocks the rest, so a gated row
    // that a human explicitly acknowledged stayed in the active queue
    // forever, just restyled.
    const isDischarged = disposition
      ? item.reason === "gated"
        ? disposition.state === "acknowledged"
        : DISCHARGED_STATES.has(disposition.state)
      : false;
    if (isDischarged) {
      discharged.push(joined);
    } else {
      active.push(joined);
    }
  }
  return { active, discharged };
}

// Board order (Running now): creation order, oldest first — never recency.
// The API sorts by updated_at (recency), which is right for Recent Runs but
// wrong here — updated_at bumps on every message, so a recency-ordered board
// reshuffles mid-conversation. started_at is the moment a run actually began
// and never changes afterward, so it's the stable key; a run can in principle
// read "running" before started_at has landed (see the QUEUED/GATED aliases
// and the unrecognized-status fallback in deriveDisplayStatus), so it falls
// back to created_at, which the sessions table guarantees is always present.
// Equal keys (same second, or two undated rows) fall back to id so the order
// is total, not "whatever the array happened to hold."
export function runCreationKey(run: RunSummary): number {
  return run.started_at ?? run.created_at ?? Number.POSITIVE_INFINITY;
}

export function invocationCreationKey(inv: InvocationSummary): number {
  return inv.started_at ?? inv.created_at ?? Number.POSITIVE_INFINITY;
}

function deriveActiveRuns(runs: RunSummary[]): RunSummary[] {
  return runs
    .filter((r) => deriveDisplayStatus(r) === "running")
    .sort(
      (a, b) =>
        runCreationKey(a) - runCreationKey(b) || runSessionId(a).localeCompare(runSessionId(b)),
    );
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
  return invocations
    .filter((i) => RUNNING_STATUSES.has(i.status.toLowerCase()))
    .sort(
      (a, b) => invocationCreationKey(a) - invocationCreationKey(b) || a.id.localeCompare(b.id),
    );
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
    dispositions: {},
    gatedPlays: [],
    attentionItems: [],
    dischargedAttentionItems: [],
    unacknowledgedAttentionCount: 0,
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
      const dispositions = action.dispositions ?? state.dispositions;
      const gatedPlays = action.gatedPlays ?? state.gatedPlays;
      const activeRuns = deriveActiveRuns(runs);
      const activeInvocations = deriveActiveInvocations(invocations);
      const recentRuns = deriveRecentRuns(runs);
      const { active: attentionItems, discharged: dischargedAttentionItems } = buildAttentionItems(
        runs,
        invocations,
        schedules,
        nowSec,
        dispositions,
        gatedPlays,
      );
      const unacknowledgedAttentionCount = attentionItems.filter((i) => !i.disposition).length;
      // A degraded schedules fetch before the first successful one leaves an
      // empty placeholder list — never declare the system empty from it.
      const systemEmpty =
        schedulesKnown &&
        runs.length === 0 &&
        invocations.length === 0 &&
        schedules.length === 0 &&
        gatedPlays.length === 0;
      return {
        ...state,
        nowSec,
        activeRuns,
        activeInvocations,
        recentRuns,
        schedules,
        schedulesKnown,
        dispositions,
        gatedPlays,
        attentionItems,
        dischargedAttentionItems,
        unacknowledgedAttentionCount,
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
