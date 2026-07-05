/**
 * Fleet view reducer.
 *
 * Join strategy: RunSummary.invocation_id → InvocationSummary.id.
 * Runs carry an optional invocation_id (set by `li invoke`). Active runs
 * with a matching invocation_id are grouped under that invocation as child
 * agent rows. Runs without a matching invocation_id land in a synthetic
 * "direct" group (id "__direct__"). Sessions are listed via
 * SessionSummary; they lack an invocation_id field in the list response,
 * so they are counted inside their parent invocation's session_count field
 * (already on InvocationSummary) rather than being individually grouped.
 * The drawer calls getSession() for per-session detail.
 *
 * Terminal/idle entries are excluded — Fleet is live-only.
 * History owns the past.
 */

import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";

// ─── Types ────────────────────────────────────────────────────────────────────

export type DataState = "loading" | "live" | "stale" | "error";

export interface AgentRow {
  id: string;
  name: string;
  status: string;
  effectiveHealth: string | null;
  elapsedSec: number | null;
  branch_count: number;
  message_count: number;
  kind: "run" | "invocation";
  invocation_id: string | null;
}

export interface OrgUnit {
  id: string;
  skill: string;
  plugin: string | null;
  status: string;
  elapsedSec: number | null;
  session_count: number;
  agents: AgentRow[];
  needsAttention: boolean;
}

export interface FleetCounts {
  orchestrations: number;
  agents: number;
  attention: number;
}

/** A terminal run kept for the idle state — Fleet shows where work just went. */
export interface RecentRow {
  id: string;
  name: string;
  status: string;
  endedAtSec: number | null;
}

export interface FleetState {
  nowSec: number;
  orgUnits: OrgUnit[];
  counts: FleetCounts;
  recent: RecentRow[];
  /** Whether the server has runs beyond the polled first page. */
  runsHasNext: boolean;
  dataState: DataState;
  lastUpdatedMs: number | null;
  errorMessage: string | null;
}

// ─── Actions ──────────────────────────────────────────────────────────────────

export type FleetAction =
  | { type: "TICK"; nowSec: number }
  | {
      type: "DATA_OK";
      invocations: InvocationSummary[];
      runs: RunSummary[];
      runsHasNext: boolean;
      nowSec: number;
    }
  | { type: "DATA_ERROR"; message: string }
  | { type: "MARK_STALE" };

// ─── Status sets ──────────────────────────────────────────────────────────────

const RUNNING_STATUSES = new Set([
  "running",
  "executing",
  "in_progress",
  "director-managed",
  "open",
]);

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

const ATTENTION_STATUSES = new Set([
  "failed",
  "error",
  "failure",
  "gated",
  "needs_review",
  "blocked",
]);

/** Health verdicts that flag a running row for attention. */
const ATTENTION_HEALTH = new Set(["unresponsive", "stale", "orphaned", "zombie"]);

// ─── Helpers ─────────────────────────────────────────────────────────────────

function elapsedSec(startedAt: number | null | undefined, nowSec: number): number | null {
  if (startedAt == null) return null;
  // started_at arrives as a float epoch — floor so display math stays integral.
  return Math.max(0, Math.floor(nowSec - startedAt));
}

function isActive(status: string): boolean {
  const s = status.toLowerCase();
  return !TERMINAL_STATUSES.has(s);
}

function needsAttention(row: AgentRow): boolean {
  const s = row.status.toLowerCase();
  if (ATTENTION_STATUSES.has(s)) return true;
  // Flag a running row on its health verdict, never its age: a days-old session
  // still emitting activity is healthy, not stuck.
  if (RUNNING_STATUSES.has(s) && row.effectiveHealth != null) {
    return ATTENTION_HEALTH.has(row.effectiveHealth);
  }
  return false;
}

// ─── Derivation ───────────────────────────────────────────────────────────────

function buildOrgUnits(
  invocations: InvocationSummary[],
  runs: RunSummary[],
  nowSec: number,
): OrgUnit[] {
  const activeInvocations = invocations.filter((inv) => isActive(inv.status));

  // Build a lookup of invocation id → index in result array
  const invMap = new Map<string, OrgUnit>();
  for (const inv of activeInvocations) {
    invMap.set(inv.id, {
      id: inv.id,
      skill: inv.skill,
      plugin: inv.plugin,
      status: inv.status,
      elapsedSec: elapsedSec(inv.started_at, nowSec),
      session_count: inv.session_count,
      agents: [],
      needsAttention: false,
    });
  }

  const directAgents: AgentRow[] = [];

  for (const run of runs) {
    if (!isActive(run.status)) continue;

    const elapsed = elapsedSec(run.started_at ?? null, nowSec);
    const row: AgentRow = {
      id: run.run_id,
      name: run.playbook_name ?? run.agent_name ?? run.run_id.slice(-12),
      status: run.status,
      effectiveHealth: run.effective_health ?? null,
      elapsedSec: elapsed,
      branch_count: run.branch_count ?? 0,
      message_count: run.message_count ?? 0,
      kind: "run",
      invocation_id: run.invocation_id ?? null,
    };

    const parent = run.invocation_id ? invMap.get(run.invocation_id) : undefined;
    if (parent) {
      parent.agents.push(row);
    } else {
      directAgents.push(row);
    }
  }

  const units: OrgUnit[] = [];

  for (const unit of invMap.values()) {
    unit.needsAttention =
      ATTENTION_STATUSES.has(unit.status.toLowerCase()) ||
      unit.agents.some((a) => needsAttention(a));
    units.push(unit);
  }

  // Sort: attention first, then by elapsed descending
  units.sort((a, b) => {
    if (a.needsAttention !== b.needsAttention) return a.needsAttention ? -1 : 1;
    return (b.elapsedSec ?? 0) - (a.elapsedSec ?? 0);
  });

  // Direct group — runs not under any invocation
  if (directAgents.length > 0) {
    directAgents.sort((a, b) => (b.elapsedSec ?? 0) - (a.elapsedSec ?? 0));
    const hasAttention = directAgents.some((a) => needsAttention(a));
    units.push({
      id: "__direct__",
      skill: "direct",
      plugin: null,
      status: "running",
      elapsedSec: null,
      session_count: directAgents.length,
      agents: directAgents,
      needsAttention: hasAttention,
    });
  }

  return units;
}

/** Terminal runs mapped to history rows, newest first. Shared with the
 *  Fleet view's lazy pagination, which maps older pages the same way. */
export function terminalRecentRows(runs: RunSummary[]): RecentRow[] {
  return runs
    .filter((r) => !isActive(r.status))
    .sort((a, b) => (b.ended_at ?? b.started_at ?? 0) - (a.ended_at ?? a.started_at ?? 0))
    .map((r) => ({
      id: r.run_id,
      name: r.playbook_name ?? r.agent_name ?? r.run_id.slice(-12),
      status: r.status,
      endedAtSec: r.ended_at ?? r.started_at ?? null,
    }));
}

/** One fetched page of older history rows. */
export interface HistoryPage {
  rows: RecentRow[];
  hasMore: boolean;
}

export interface HistoryPager {
  inFlight(): boolean;
  loadNext(): Promise<HistoryPage | null>;
}

/**
 * Serializes on-demand history page fetches. The in-flight guard is plain
 * closure state, flipped synchronously — React render state stays stale until
 * commit, so two fires in the same tick (sentinel intersection plus a click)
 * would otherwise both fetch the same page and double-advance past the next
 * one. A concurrent call resolves to null without fetching; a failed fetch
 * keeps its page number so the next fire retries it.
 */
export function createHistoryPager(
  fetchPage: (page: number) => Promise<{ runs: RunSummary[]; has_next: boolean }>,
  firstPage = 2,
): HistoryPager {
  let nextPage = firstPage;
  let inFlight = false;
  return {
    inFlight: () => inFlight,
    loadNext() {
      if (inFlight) return Promise.resolve(null);
      inFlight = true;
      const page = nextPage;
      nextPage = page + 1;
      return fetchPage(page)
        .then((resp) => ({ rows: terminalRecentRows(resp.runs), hasMore: resp.has_next }))
        .catch(() => {
          nextPage = page;
          return null;
        })
        .finally(() => {
          inFlight = false;
        });
    },
  };
}

function deriveCounts(units: OrgUnit[]): FleetCounts {
  const orchestrations = units.filter((u) => u.id !== "__direct__").length;
  const agents = units.reduce((n, u) => n + u.agents.length, 0);
  const attention = units.reduce((n, u) => {
    if (u.id === "__direct__") return n + u.agents.filter((a) => needsAttention(a)).length;
    return n + (u.needsAttention ? 1 : 0);
  }, 0);
  return { orchestrations, agents, attention };
}

// ─── Initial state ────────────────────────────────────────────────────────────

export function initialFleetState(): FleetState {
  return {
    nowSec: Math.floor(Date.now() / 1000),
    orgUnits: [],
    counts: { orchestrations: 0, agents: 0, attention: 0 },
    recent: [],
    runsHasNext: false,
    dataState: "loading",
    lastUpdatedMs: null,
    errorMessage: null,
  };
}

// ─── Reducer ─────────────────────────────────────────────────────────────────

export function fleetReducer(state: FleetState, action: FleetAction): FleetState {
  switch (action.type) {
    case "TICK":
      return { ...state, nowSec: action.nowSec };

    case "DATA_OK": {
      const { invocations, runs, runsHasNext, nowSec } = action;
      const orgUnits = buildOrgUnits(invocations, runs, nowSec);
      const counts = deriveCounts(orgUnits);
      return {
        ...state,
        nowSec,
        orgUnits,
        counts,
        recent: terminalRecentRows(runs),
        runsHasNext,
        dataState: "live",
        lastUpdatedMs: Date.now(),
        errorMessage: null,
      };
    }

    case "DATA_ERROR":
      return { ...state, dataState: "error", errorMessage: action.message };

    case "MARK_STALE":
      if (state.dataState === "live") {
        return { ...state, dataState: "stale" };
      }
      return state;

    default:
      return state;
  }
}
