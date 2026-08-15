// Shared, framework-free state primitives for the execution-graph progress
// surface (progress summary counts, elapsed timer, descendant-terminal
// status reconciliation, and stage/rank position). Every function here reads
// from the SAME canonical status source the graph nodes render from
// (`Record<string, NodeExecStatus>`, keyed by authored node id, as built by
// `buildNodeStatusesByName` in `./operationGraph.ts`) — a header or badge
// built from these functions cannot disagree with what a node displays,
// because both derive from one map.
import type { NodeExecStatus } from "@/components/canvas/StepNode";

export type NodeStatusMap = Record<string, NodeExecStatus>;

export interface GraphEdge {
  source: string;
  target: string;
}

// ── Progress summary counts ─────────────────────────────────────────────────

export interface ProgressCounts {
  total: number;
  pending: number;
  queued: number;
  running: number;
  awaitingApproval: number;
  paused: number;
  completed: number;
  skipped: number;
  cancelled: number;
  escalated: number;
  failed: number;
  hasFailure: boolean;
}

function emptyCounts(total = 0): ProgressCounts {
  return {
    total,
    pending: 0,
    queued: 0,
    running: 0,
    awaitingApproval: 0,
    paused: 0,
    completed: 0,
    skipped: 0,
    cancelled: 0,
    escalated: 0,
    failed: 0,
    hasFailure: false,
  };
}

// nodeIds is the authored node id list (the total a viewer expects to see);
// statuses only covers nodes with live signal correlation — a node absent
// from it defaults to "pending", exactly as WorkerCanvas does for graph
// nodes, so the header and the nodes can never diverge on what "no signal"
// means.
export function deriveProgressCounts(
  nodeIds: string[],
  statuses: NodeStatusMap | undefined,
): ProgressCounts {
  const counts = emptyCounts(nodeIds.length);
  for (const id of nodeIds) {
    const status = statuses?.[id] ?? "pending";
    switch (status) {
      case "pending":
        counts.pending++;
        break;
      case "queued":
        counts.queued++;
        break;
      case "running":
        counts.running++;
        break;
      case "awaiting_approval":
        counts.awaitingApproval++;
        break;
      case "paused":
        counts.paused++;
        break;
      case "completed":
        counts.completed++;
        break;
      case "failed":
        counts.failed++;
        break;
      // Counted in its own bucket, never folded into completed or failed: a
      // status with no case here is counted in nothing, and the buckets stop
      // summing to total.
      case "skipped":
        counts.skipped++;
        break;
      case "cancelled":
        counts.cancelled++;
        break;
      case "escalated":
        counts.escalated++;
        break;
    }
  }
  counts.hasFailure = counts.failed > 0;
  return counts;
}

// ── Elapsed wall-clock ───────────────────────────────────────────────────────

// `now` is passed in rather than read internally so this stays pure and
// testable; a live caller re-derives it on a 1s interval.
export function computeElapsedSeconds(
  startedAt: number | null | undefined,
  endedAt: number | null | undefined,
  now: number,
): number | null {
  if (startedAt == null) return null;
  const end = endedAt ?? now;
  return Math.max(0, end - startedAt);
}

export function formatElapsed(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "--:--";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// ── Descendant-terminal suppression + terminal-run collapse ────────────────

const TERMINAL_STATUSES = new Set<NodeExecStatus>([
  "completed",
  "failed",
  "skipped",
  "cancelled",
  "escalated",
]);
const NON_TERMINAL_ACTIVE_STATUSES = new Set<NodeExecStatus>([
  "queued",
  "running",
  "awaiting_approval",
  "paused",
]);

function buildDescendantIndex(nodeIds: string[], edges: GraphEdge[]): Map<string, Set<string>> {
  const known = new Set(nodeIds);
  const outEdges = new Map<string, string[]>();
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue;
    (outEdges.get(e.source) ?? outEdges.set(e.source, []).get(e.source)!).push(e.target);
  }

  const cache = new Map<string, Set<string>>();
  const inProgress = new Set<string>();
  const descendantsOf = (id: string): Set<string> => {
    const cached = cache.get(id);
    if (cached) return cached;
    if (inProgress.has(id)) return new Set(); // cycle guard — graph is expected acyclic
    inProgress.add(id);
    const result = new Set<string>();
    for (const next of outEdges.get(id) ?? []) {
      result.add(next);
      for (const d of descendantsOf(next)) result.add(d);
    }
    inProgress.delete(id);
    cache.set(id, result);
    return result;
  };

  const index = new Map<string, Set<string>>();
  for (const id of nodeIds) index.set(id, descendantsOf(id));
  return index;
}

// Two invariants a viewer actually relies on, applied in order:
//
// 1. Descendant-terminal suppression (holds regardless of `done`): a node
//    cannot still be "running" once a descendant has reached a terminal
//    state — the descendant could not have started without this node's
//    output, so the stale "running" reading is corrected to "completed".
// 2. Terminal-run collapse (only once `done`): after the run itself has
//    ended, no node may present as still in flight. Any status that is
//    still non-terminal at that point means "no terminal signal was ever
//    recorded for this node" — which must read as absence of information
//    ("pending"), never be left looking like live work.
export function reconcileNodeStatuses(
  nodeIds: string[],
  edges: GraphEdge[],
  statuses: NodeStatusMap | undefined,
  done: boolean,
  failedNodeIds?: ReadonlySet<string>,
): NodeStatusMap {
  const base: NodeStatusMap = {};
  for (const id of nodeIds) base[id] = statuses?.[id] ?? "pending";
  // The run's own failure evidence names the op(s) that killed it. That
  // verdict outranks whatever lifecycle signal the dying engine managed to
  // record — the named op often still reads "queued" or "running" because
  // the engine never got to emit its terminal signal.
  if (failedNodeIds) {
    for (const id of nodeIds) {
      if (failedNodeIds.has(id)) base[id] = "failed";
    }
  }

  const descendants = buildDescendantIndex(nodeIds, edges);
  const afterSuppression: NodeStatusMap = { ...base };
  for (const id of nodeIds) {
    if (afterSuppression[id] !== "running") continue;
    let hasTerminalDescendant = false;
    for (const d of descendants.get(id) ?? []) {
      if (TERMINAL_STATUSES.has(base[d]!)) {
        hasTerminalDescendant = true;
        break;
      }
    }
    if (hasTerminalDescendant) afterSuppression[id] = "completed";
  }

  if (!done) return afterSuppression;

  const final: NodeStatusMap = {};
  for (const id of nodeIds) {
    const status = afterSuppression[id]!;
    final[id] = NON_TERMINAL_ACTIVE_STATUSES.has(status) ? "pending" : status;
  }
  return final;
}

// ── Stage / rank position ───────────────────────────────────────────────────

// Longest-path layering over the AUTHORED edge set (never the transitively
// reduced one) so the stage-of-record stays honest under reduction — display
// edges may drop, ranks-of-record never do. Mirrors
// OperationGraphSection.computeLayers, generalized from OperationNode/opId to
// bare node ids so it can run over a planned WorkerGraph.
export function computeAuthoredLayers(nodeIds: string[], edges: GraphEdge[]): string[][] {
  if (nodeIds.length === 0) return [];

  const known = new Set(nodeIds);
  const predsByNode = new Map<string, string[]>();
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue;
    (predsByNode.get(e.target) ?? predsByNode.set(e.target, []).get(e.target)!).push(e.source);
  }

  const depthCache = new Map<string, number>();
  const onStack = new Set<string>();
  const depthOf = (id: string): number => {
    const cached = depthCache.get(id);
    if (cached !== undefined) return cached;
    const preds = predsByNode.get(id);
    if (!preds || preds.length === 0) {
      depthCache.set(id, 0);
      return 0;
    }
    if (onStack.has(id)) return 0; // cycle guard — graph is expected acyclic
    onStack.add(id);
    let depth = 0;
    for (const p of preds) depth = Math.max(depth, depthOf(p) + 1);
    onStack.delete(id);
    depthCache.set(id, depth);
    return depth;
  };

  const depths = nodeIds.map((id) => depthOf(id));
  const maxDepth = Math.max(...depths);
  const layers: string[][] = Array.from({ length: maxDepth + 1 }, () => []);
  nodeIds.forEach((id, i) => layers[depths[i]!]!.push(id));
  return layers;
}

export interface StagePosition {
  /** 1-based; 0 when there are no nodes at all. */
  stage: number;
  totalStages: number;
}

// Live stage = the layer containing the highest-numbered running node; once
// `done`, the last layer with a completed node (or the final stage if none
// is marked completed — e.g. a run that failed before any node finished
// still reports the deepest reachable stage as "reached").
export function computeStagePosition(
  nodeIds: string[],
  edges: GraphEdge[],
  statuses: NodeStatusMap | undefined,
  done: boolean,
): StagePosition {
  const layers = computeAuthoredLayers(nodeIds, edges);
  const totalStages = layers.length;
  if (totalStages === 0) return { stage: 0, totalStages: 0 };

  const statusOf = (id: string): NodeExecStatus => statuses?.[id] ?? "pending";

  if (done) {
    for (let i = layers.length - 1; i >= 0; i--) {
      if (layers[i]!.some((id) => statusOf(id) === "completed")) {
        return { stage: i + 1, totalStages };
      }
    }
    return { stage: totalStages, totalStages };
  }

  for (let i = layers.length - 1; i >= 0; i--) {
    if (layers[i]!.some((id) => statusOf(id) === "running")) {
      return { stage: i + 1, totalStages };
    }
  }
  for (let i = layers.length - 1; i >= 0; i--) {
    if (layers[i]!.some((id) => statusOf(id) === "completed")) {
      return { stage: i + 1, totalStages };
    }
  }
  return { stage: 1, totalStages };
}
