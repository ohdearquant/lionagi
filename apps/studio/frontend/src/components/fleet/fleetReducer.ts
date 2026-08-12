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
 *
 * The active-snapshot endpoint applies project/search scope to invocation
 * membership through matching child sessions, then returns exact active
 * totals alongside bounded rows. The reducer therefore never treats a page
 * boundary as proof that no matching work exists.
 */

import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";
import { deriveDisplayStatus, isEffectivelyActive, type RunStatusInput } from "@/lib/runStatus";
import { resolveRunLabel } from "@/lib/runLabel";

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
  // agent | play | flow | fanout | show-play (issue #2842) — the discriminator
  // that says whether this row is a single agent or the root of a multi-agent
  // execution. `agentName`/label text alone never establishes that.
  invocationKind: string | null;
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
  invocation_id: string | null;
  status_reason_code?: string | null;
  status_reason_summary?: string | null;
  endedAtSec: number | null;
  // Cost-visibility contract: `null` means unreported (unknown), never a
  // coerced 0 — format with usageFormat.ts, don't branch on truthiness.
  totalCostUsd: number | null;
}

export interface FleetState {
  nowSec: number;
  orgUnits: OrgUnit[];
  counts: FleetCounts;
  recent: RecentRow[];
  /** Whether terminal history has rows beyond the snapshot's first page. */
  runsHasNext: boolean;
  activeRunTotal: number;
  activeInvocationTotal: number;
  activeRunOmitted: number;
  activeInvocationOmitted: number;
  snapshotVersion: string | null;
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
      activeRunTotal?: number;
      activeInvocationTotal?: number;
      activeRunOmitted?: number;
      activeInvocationOmitted?: number;
      snapshotVersion?: string;
      nowSec: number;
      /** Same filters just sent to the active snapshot — determines whether an
       *  invocation group with no matching child should be suppressed. */
      project?: string;
      projectNull?: boolean;
      search?: string;
    }
  | { type: "DATA_ERROR"; message: string }
  | { type: "MARK_STALE" };

// ─── Status sets ──────────────────────────────────────────────────────────────

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

// Active/terminal is the same lifecycle axis RunDetail and the mission board
// use — route it through the one shared classifier so Fleet never drifts
// from either (the exact list-vs-detail bug this closes, on the fleet view).
// Also excludes running rows the shared health classifier has confirmed
// dead (stale/orphaned/zombie), so a killed process can't count as an
// active Fleet agent just because its DB status column still says running.
function isActive(entity: RunStatusInput): boolean {
  return isEffectivelyActive(entity);
}

function needsAttention(row: AgentRow): boolean {
  const s = row.status.toLowerCase();
  if (ATTENTION_STATUSES.has(s)) return true;
  // Flag a running row on its health verdict, never its age: a days-old session
  // still emitting activity is healthy, not stuck.
  if (deriveDisplayStatus(row) === "running" && row.effectiveHealth != null) {
    return ATTENTION_HEALTH.has(row.effectiveHealth);
  }
  return false;
}

// ─── Derivation ───────────────────────────────────────────────────────────────

/** True when the runs projection is scoped to less than "everything live" —
 *  the same condition useFleet uses to decide what to send listRuns(). */
function isScopeActive(project?: string, projectNull?: boolean, search?: string): boolean {
  return Boolean(project) || Boolean(projectNull) || Boolean(search);
}

function buildOrgUnits(
  invocations: InvocationSummary[],
  runs: RunSummary[],
  nowSec: number,
  scoped: boolean,
  runsHasNext: boolean,
  scopeIsServerCoherent: boolean,
): OrgUnit[] {
  const activeInvocations = invocations.filter((inv) => isActive(inv));

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
    if (!isActive(run)) continue;

    const elapsed = elapsedSec(run.started_at ?? null, nowSec);
    const row: AgentRow = {
      id: run.run_id,
      name: resolveRunLabel(run),
      status: run.status,
      effectiveHealth: run.effective_health ?? null,
      elapsedSec: elapsed,
      branch_count: run.branch_count ?? 0,
      message_count: run.message_count ?? 0,
      kind: "run",
      invocation_id: run.invocation_id ?? null,
      invocationKind: run.invocation_kind ?? null,
    };

    const parent = run.invocation_id ? invMap.get(run.invocation_id) : undefined;
    if (parent) {
      parent.agents.push(row);
    } else {
      directAgents.push(row);
    }
  }

  const units: OrgUnit[] = [];

  // Legacy callers supplied independently fetched invocations and runs, so a
  // scoped, exhaustive runs page was the only evidence that an empty invocation
  // did not belong. Snapshot callers are different: invocation membership was
  // scoped transactionally on the server. Keep those groups even when their
  // matching child fell beyond the bounded active-runs page.
  const runsAreExhaustive = !runsHasNext;
  for (const unit of invMap.values()) {
    if (scoped && !scopeIsServerCoherent && runsAreExhaustive && unit.agents.length === 0) continue;
    // A scoped view reports the children it is showing. The invocation's own
    // session_count is global, so rendering it beside a filtered child list
    // states a total that belongs to a different question.
    if (scoped) unit.session_count = unit.agents.length;
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

function mapRunsToRecentRows(runs: RunSummary[]): RecentRow[] {
  return runs
    .filter((r) => !isActive(r))
    .map((r) => ({
      id: r.run_id,
      name: resolveRunLabel(r),
      status: r.status,
      invocation_id: r.invocation_id ?? null,
      status_reason_code: r.status_reason_code,
      status_reason_summary: r.status_reason_summary,
      endedAtSec: r.ended_at ?? r.started_at ?? null,
      totalCostUsd: r.total_cost_usd ?? null,
    }));
}

/** Terminal runs mapped to history rows, newest first. Shared with the
 *  Fleet view's lazy pagination, which maps older pages the same way. */
export function terminalRecentRows(runs: RunSummary[]): RecentRow[] {
  return mapRunsToRecentRows(runs).sort((a, b) => (b.endedAtSec ?? 0) - (a.endedAtSec ?? 0));
}

/** Terminal runs mapped to history rows, preserving the server's own order —
 *  for the "Highest cost" history sort, where /api/runs/?sort=cost has
 *  already computed the ordering and a client re-sort by end time would
 *  silently undo it. */
export function terminalRecentRowsServerOrder(runs: RunSummary[]): RecentRow[] {
  return mapRunsToRecentRows(runs);
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
 *
 * mapRows defaults to the recency ordering; the "Highest cost" history sort
 * passes terminalRecentRowsServerOrder so later pages stay in the server's
 * cost order instead of being re-sorted by end time.
 */
export function createHistoryPager(
  fetchPage: (page: number) => Promise<{ runs: RunSummary[]; has_next: boolean }>,
  firstPage = 2,
  mapRows: (runs: RunSummary[]) => RecentRow[] = terminalRecentRows,
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
        .then((resp) => ({ rows: mapRows(resp.runs), hasMore: resp.has_next }))
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

const ORCHESTRATION_KINDS = new Set(["play", "fanout", "flow"]);

/**
 * An orchestration is counted once, from whichever evidence exists for it.
 *
 * A group is the better evidence, but a group only forms when a run carries
 * invocation_id, and that field is populated on the `li invoke` path alone. A
 * play, fanout or flow never sets it, so it formed no group and was invisible
 * here: the strip read zero orchestrations while one was visibly running in the
 * list directly below it. Counting those runs closes that gap without inventing
 * a parentage link that does not exist — it makes the number honest, not the
 * tree. Their workers stay ungrouped, because rendering them as affiliated
 * would be the same falsehood moved somewhere harder to notice.
 *
 * Runs already inside a group are excluded so this stays a union rather than a
 * double count. Once real parentage exists, every orchestration forms a group,
 * the second term goes to zero, and this reduces to the group count on its own.
 */
function deriveCounts(units: OrgUnit[], runs: RunSummary[]): FleetCounts {
  const groups = units.filter((u) => u.id !== "__direct__");
  const grouped = new Set(groups.flatMap((u) => u.agents.map((a) => a.id)));
  const ungroupedOrchestrations = runs.filter(
    (r) =>
      isActive(r) && ORCHESTRATION_KINDS.has(r.invocation_kind ?? "") && !grouped.has(r.run_id),
  ).length;

  const orchestrations = groups.length + ungroupedOrchestrations;
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
    activeRunTotal: 0,
    activeInvocationTotal: 0,
    activeRunOmitted: 0,
    activeInvocationOmitted: 0,
    snapshotVersion: null,
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
      const { invocations, runs, runsHasNext, nowSec, project, projectNull, search } = action;
      const scoped = isScopeActive(project, projectNull, search);
      const orgUnits = buildOrgUnits(
        invocations,
        runs,
        nowSec,
        scoped,
        runsHasNext,
        action.snapshotVersion != null,
      );
      const counts = deriveCounts(orgUnits, runs);
      const displayedActiveRuns = runs.filter(isActive).length;
      const displayedActiveInvocations = invocations.filter(isActive).length;
      const activeRunTotal = action.activeRunTotal ?? displayedActiveRuns;
      const activeInvocationTotal = action.activeInvocationTotal ?? displayedActiveInvocations;
      return {
        ...state,
        nowSec,
        orgUnits,
        counts,
        recent: terminalRecentRows(runs),
        runsHasNext,
        activeRunTotal,
        activeInvocationTotal,
        activeRunOmitted:
          action.activeRunOmitted ?? Math.max(0, activeRunTotal - displayedActiveRuns),
        activeInvocationOmitted:
          action.activeInvocationOmitted ??
          Math.max(0, activeInvocationTotal - displayedActiveInvocations),
        snapshotVersion: action.snapshotVersion ?? state.snapshotVersion,
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
