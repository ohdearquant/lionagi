import type { SignalEvent } from "./api";

// ── Types ─────────────────────────────────────────────────────────────────────

export type OperationStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "paused"
  | "succeeded"
  | "failed"
  | "skipped"
  | "escalated";

export interface OperationNode {
  opId: string;
  name: string;
  status: OperationStatus;
  causeOpId: string | null;
  elapsed: number;
  firstTs: number;
  lastTs: number;
  eventCount: number;
}

export interface OperationEdge {
  source: string;
  target: string;
  continuation?: boolean;
}

export interface OperationGraphState {
  nodes: OperationNode[];
  edges: OperationEdge[];
}

// ── Status projection ─────────────────────────────────────────────────────────

const TERMINAL = new Set<OperationStatus>(["succeeded", "failed", "skipped", "escalated"]);

const KIND_TO_STATE: Record<string, OperationStatus | undefined> = {
  NodeQueued: "queued",
  NodeStarted: "running",
  NodeAwaitingApproval: "awaiting_approval",
  NodePaused: "paused",
  NodeCompleted: "succeeded",
  NodeFailed: "failed",
  // A node an edge condition passed over. Distinct from NodeFailed on
  // purpose: it never ran, so presenting it as an error misreads a working
  // gate as a broken step. Any kind missing from this map falls through the
  // `if (!newState) continue` below and leaves the node at its initial
  // "queued", so a backend signal added without a row here reads as a node
  // that never finished -- which is why this mirrors _NODE_KIND_TO_STATE in
  // lionagi/studio/operator/run_progress.py entry for entry.
  NodeSkipped: "skipped",
  NodeEscalated: "escalated",
};

/** Return the lifecycle state carried by one signal kind, if it is a node
 * lifecycle signal.  Incremental projections use this instead of keeping the
 * raw signal stream solely to rediscover the same mapping. */
export function operationStatusForSignal(kind: string): OperationStatus | undefined {
  return KIND_TO_STATE[kind];
}

// A lane-projection input: either a bare kind string (back-compat; an
// unaccompanied "NodeEscalated" with no route info still projects to
// "escalated", matching an EscalationRequest-less signal on the backend) or
// a {kind, route} pair carrying the NodeEscalated route so a soft ("fyi")
// help signal (route="notify") can be told apart from a real escalation.
export type LaneSignal = string | { kind: string; route?: string };

/** Fold one lifecycle signal into an existing lane state.  This is the
 * single-event form of laneFor(), exported for bounded incremental indexes. */
export function advanceLane(current: OperationStatus, entry: LaneSignal): OperationStatus {
  const kind = typeof entry === "string" ? entry : entry.kind;
  const route = typeof entry === "string" ? undefined : entry.route;
  if (kind === "NodeEscalated" && route === "notify") return current;
  const next = KIND_TO_STATE[kind];
  if (!next) return current;
  if (TERMINAL.has(current) && next !== "queued" && next !== "running") return current;
  return next;
}

export function laneFor(kinds: LaneSignal[]): OperationStatus {
  let state: OperationStatus = "queued";
  for (const entry of kinds) {
    state = advanceLane(state, entry);
  }
  return state;
}

// Build the laneFor() input for one signal event, carrying its route when
// present (currently only NodeEscalated sets it) so laneFor can tell a soft
// help signal apart from a real escalation.
function toLaneSignal(ev: SignalEvent): LaneSignal {
  const route = ev.payload?.route;
  return typeof route === "string" ? { kind: ev.kind, route } : ev.kind;
}

// ── Transitive reduction ──────────────────────────────────────────────────────

// The engine emits one `depends_on` entry per graph predecessor, which can
// include indirect ancestors (e.g. A→B→C also lists A as a dependency of C).
// Rendered as edges that draws A→C alongside A→B→C, which is redundant and
// clutters the DAG. Drop an edge u→v whenever v is already reachable from u
// through some other edge out of u (a path of length ≥2). Cycle-guarded via
// an in-progress set — the graph is expected to be acyclic, but a stray cycle
// must not hang the reducer.
export function transitiveReduce<E extends { source: string; target: string }>(edges: E[]): E[] {
  if (edges.length === 0) return edges;

  const outEdges = new Map<string, E[]>();
  for (const e of edges) {
    (outEdges.get(e.source) ?? outEdges.set(e.source, []).get(e.source)!).push(e);
  }

  const reachableCache = new Map<string, Set<string>>();
  const inProgress = new Set<string>();
  const reachableFrom = (node: string): Set<string> => {
    const cached = reachableCache.get(node);
    if (cached) return cached;
    if (inProgress.has(node)) return new Set(); // cycle guard
    inProgress.add(node);
    const result = new Set<string>();
    for (const e of outEdges.get(node) ?? []) {
      result.add(e.target);
      for (const r of reachableFrom(e.target)) result.add(r);
    }
    inProgress.delete(node);
    reachableCache.set(node, result);
    return result;
  };

  return edges.filter((e) => {
    for (const alt of outEdges.get(e.source) ?? []) {
      if (alt === e || alt.target === e.target) continue;
      if (reachableFrom(alt.target).has(e.target)) return false; // redundant
    }
    return true;
  });
}

// ── Display-time transitive reduction with semantic guard ──────────────────────

// An authored WorkerGraph edge can carry semantics beyond structure — a
// condition, a map, a handler, or mode="code" — matching the richness
// criteria in resolveGraphEdges (RunDetail.tsx). Such an edge is information
// the designer put there on purpose, not a redundant ancestor link, so it
// must never be dropped even when another path already reaches its target.
function defaultIsRich(e: {
  condition?: unknown;
  map?: unknown;
  handler?: unknown;
  mode?: unknown;
}): boolean {
  return Boolean(e.condition) || Boolean(e.map) || Boolean(e.handler) || e.mode === "code";
}

// Whole-graph directed-cycle check (3-color DFS). Unlike transitiveReduce's
// per-node inProgress guard — which silently treats a cyclic node as
// unreachable and can still drop edges around it — this is a guard for the
// entire reduction: depends_on graphs are expected to be acyclic, but if a
// cycle is found every edge in it may be load-bearing for the cycle itself,
// so reduction must not run at all.
export function hasCycle<E extends { source: string; target: string }>(edges: E[]): boolean {
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    (adj.get(e.source) ?? adj.set(e.source, []).get(e.source)!).push(e.target);
  }

  const WHITE = 0,
    GRAY = 1,
    BLACK = 2;
  const color = new Map<string, number>();

  const dfs = (node: string): boolean => {
    const c = color.get(node) ?? WHITE;
    if (c === GRAY) return true;
    if (c === BLACK) return false;
    color.set(node, GRAY);
    for (const next of adj.get(node) ?? []) {
      if (dfs(next)) return true;
    }
    color.set(node, BLACK);
    return false;
  };

  for (const node of adj.keys()) {
    if (color.get(node) === undefined && dfs(node)) return true;
  }
  return false;
}

// Display-time transitive reduction for an authored WorkerGraph's edges.
// Drops edge (u→v) only when v is reachable from u via a path of length ≥2
// where EVERY edge on that path is itself structurally plain and connects
// two visible nodes. Three guards distinguish this from transitiveReduce
// above:
//   1. Whole-graph cycle detection — a cyclic graph is returned unchanged
//      rather than reduced around the cycle.
//   2. Semantically rich edges (see defaultIsRich) are never dropped, and
//      never count as a step of an implying path — a condition/map/handler/
//      code edge is not an unconditional implication, so it cannot stand in
//      for the plain edge it might otherwise seem to make redundant.
//   3. A path that passes through a node the caller says will not be
//      rendered (options.visibleNodes) cannot justify hiding an edge either
//      — a viewer can't confirm an implication they can't see resolve.
// Never mutates or re-persists the input; this only affects what is
// rendered.
export function transitiveReduceDisplay<E extends { source: string; target: string }>(
  edges: E[],
  options?: { isRich?: (e: E) => boolean; visibleNodes?: Set<string> },
): { kept: E[]; hidden: E[] } {
  if (edges.length === 0) return { kept: [], hidden: [] };
  if (hasCycle(edges)) return { kept: edges, hidden: [] };

  const isRich = options?.isRich ?? defaultIsRich;
  const visibleNodes = options?.visibleNodes;
  const isVisible = (id: string) => !visibleNodes || visibleNodes.has(id);

  const rich: E[] = [];
  const plain: E[] = [];
  for (const e of edges) {
    (isRich(e) ? rich : plain).push(e);
  }

  // Only a plain edge between two visible nodes can be one step of an
  // implying path — a rich edge's implication is conditional, and a step
  // through a hidden node is not one the viewer can see resolve.
  const plainOutEdges = new Map<string, E[]>();
  for (const e of plain) {
    if (!isVisible(e.source) || !isVisible(e.target)) continue;
    (plainOutEdges.get(e.source) ?? plainOutEdges.set(e.source, []).get(e.source)!).push(e);
  }

  const reachableCache = new Map<string, Set<string>>();
  const reachableFrom = (node: string): Set<string> => {
    const cached = reachableCache.get(node);
    if (cached) return cached;
    const result = new Set<string>();
    reachableCache.set(node, result); // seed before recursing; the graph is acyclic here
    for (const e of plainOutEdges.get(node) ?? []) {
      result.add(e.target);
      for (const r of reachableFrom(e.target)) result.add(r);
    }
    return result;
  };

  const hidden: E[] = [];
  const survivingPlain = plain.filter((e) => {
    // An edge touching a hidden node can't be verified as implied by a
    // visible path either — keep it rather than guess.
    if (!isVisible(e.source) || !isVisible(e.target)) return true;
    for (const alt of plainOutEdges.get(e.source) ?? []) {
      if (alt === e || alt.target === e.target) continue;
      if (reachableFrom(alt.target).has(e.target)) {
        hidden.push(e);
        return false;
      }
    }
    return true;
  });

  return { kept: [...rich, ...survivingPlain], hidden };
}

// ── Graph builder ─────────────────────────────────────────────────────────────

export function buildOperationGraph(events: SignalEvent[]): OperationGraphState {
  const order: string[] = [];
  const kindsByOp = new Map<string, LaneSignal[]>();
  const nameByOp = new Map<string, string>();
  const elapsedByOp = new Map<string, number>();
  const firstTsByOp = new Map<string, number>();
  const lastTsByOp = new Map<string, number>();
  const causeOpIdByOp = new Map<string, string | null>();
  const edgeSet = new Set<string>();
  const independentSpawnOrigins = new Map<string, { source: string; target: string }>();
  const higherTierEscalations = new Set<string>();

  for (const ev of events) {
    if (!ev.op_id) continue;

    if (ev.kind === "NodeSpawned") {
      const parentId = ev.payload?.parent_id;
      if (
        ev.payload?.independent === true &&
        typeof parentId === "string" &&
        parentId &&
        parentId !== ev.op_id
      ) {
        const key = `${parentId}→${ev.op_id}`;
        independentSpawnOrigins.set(key, { source: parentId, target: ev.op_id });
      }
      continue;
    }

    if (!KIND_TO_STATE[ev.kind]) continue;

    if (ev.kind === "NodeEscalated" && ev.payload?.route === "higher_tier") {
      higherTierEscalations.add(ev.op_id);
    }

    if (!kindsByOp.has(ev.op_id)) {
      kindsByOp.set(ev.op_id, []);
      order.push(ev.op_id);
      causeOpIdByOp.set(ev.op_id, null);
    }

    kindsByOp.get(ev.op_id)!.push(toLaneSignal(ev));

    const ts = ev.ts;
    if (!firstTsByOp.has(ev.op_id) || ts < firstTsByOp.get(ev.op_id)!) {
      firstTsByOp.set(ev.op_id, ts);
    }
    if (!lastTsByOp.has(ev.op_id) || ts > lastTsByOp.get(ev.op_id)!) {
      lastTsByOp.set(ev.op_id, ts);
    }

    const payload = ev.payload;
    if (payload) {
      const name = payload.name;
      if (typeof name === "string" && name && !nameByOp.has(ev.op_id)) {
        nameByOp.set(ev.op_id, name);
      }
      const elapsed = payload.elapsed;
      if (typeof elapsed === "number") {
        const prev = elapsedByOp.get(ev.op_id) ?? 0;
        if (elapsed > prev) elapsedByOp.set(ev.op_id, elapsed);
      }
      // The engine emits `depends_on` (all graph predecessors) and `parent_id`
      // (the sole predecessor, when there is exactly one) on every Node* signal;
      // some emitters instead set the singular `cause_op_id`. Read all three so
      // the run DAG renders edges regardless of which the emitter populated.
      const causeOpId = payload.cause_op_id;
      const parentId = payload.parent_id;
      const primaryCause =
        (typeof causeOpId === "string" && causeOpId) ||
        (typeof parentId === "string" && parentId) ||
        null;
      if (primaryCause) {
        if (!causeOpIdByOp.get(ev.op_id)) causeOpIdByOp.set(ev.op_id, primaryCause);
        if (primaryCause !== ev.op_id) edgeSet.add(`${primaryCause}→${ev.op_id}`);
      }
      const dependsOn = payload.depends_on;
      if (Array.isArray(dependsOn)) {
        for (const dep of dependsOn) {
          if (typeof dep === "string" && dep && dep !== ev.op_id) {
            edgeSet.add(`${dep}→${ev.op_id}`);
          }
        }
      }
    }
  }

  const continuationEdges: OperationEdge[] = [];
  for (const [key, origin] of independentSpawnOrigins) {
    // Only an escalation retry gets its parent link reclassified. Everything
    // else that spawns independently keeps whatever the lifecycle signals
    // said about it, including a plain dependency edge repeating the parent.
    if (!higherTierEscalations.has(origin.source)) continue;

    // For a retry, the parent link is causal provenance rather than a
    // scheduling dependency, so the repeated plain edge is removed and the
    // cause cleared before the continuation replaces them.
    edgeSet.delete(key);
    causeOpIdByOp.set(origin.target, null);

    continuationEdges.push({ ...origin, continuation: true });

    const originName = nameByOp.get(origin.source);
    const childName = nameByOp.get(origin.target);
    const originHasReadableName = Boolean(originName) && originName !== origin.source.slice(0, 8);
    const childHasFallbackName = !childName || childName === origin.target.slice(0, 8);
    if (originHasReadableName && childHasFallbackName) {
      nameByOp.set(origin.target, `${originName} escalation retry`);
    }
  }

  const nodes: OperationNode[] = order.map((opId) => ({
    opId,
    name: nameByOp.get(opId) ?? "",
    status: laneFor(kindsByOp.get(opId) ?? []),
    causeOpId: causeOpIdByOp.get(opId) ?? null,
    elapsed: elapsedByOp.get(opId) ?? 0,
    firstTs: firstTsByOp.get(opId) ?? 0,
    lastTs: lastTsByOp.get(opId) ?? 0,
    eventCount: (kindsByOp.get(opId) ?? []).length,
  }));

  // Continuations are annotative provenance, so they neither participate in
  // dependency reachability nor get removed as transitively redundant.
  const edges: OperationEdge[] = [
    ...transitiveReduce(
      Array.from(edgeSet).map((key) => {
        const [source, target] = key.split("→");
        return { source: source!, target: target! };
      }),
    ),
    ...continuationEdges,
  ];

  return { nodes, edges };
}

// ── Correlation against a planned (authored) graph ────────────────────────────

// The engine's Node* signals carry the runtime Operation UUID as `op_id` and,
// when the node has an authored id (a Studio designer box, or a role/step name
// from a planner), the authored id as `payload.name`. A planned WorkerGraph's
// node ids ARE those authored names — so live status must correlate on `name`,
// never on `op_id` (which the planned graph knows nothing about).
export interface NodeSignalStatus {
  status: OperationStatus;
  elapsed: number;
  eventCount: number;
}

export function buildNodeStatusesByName(events: SignalEvent[]): Map<string, NodeSignalStatus> {
  const kindsByName = new Map<string, LaneSignal[]>();
  const elapsedByName = new Map<string, number>();

  for (const ev of events) {
    if (!KIND_TO_STATE[ev.kind]) continue;
    const payload = ev.payload;
    const name = payload && typeof payload.name === "string" ? payload.name : "";
    if (!name) continue;

    (kindsByName.get(name) ?? kindsByName.set(name, []).get(name)!).push(toLaneSignal(ev));

    const elapsed = payload?.elapsed;
    if (typeof elapsed === "number") {
      const prev = elapsedByName.get(name) ?? 0;
      if (elapsed > prev) elapsedByName.set(name, elapsed);
    }
  }

  const result = new Map<string, NodeSignalStatus>();
  for (const [name, kinds] of kindsByName) {
    result.set(name, {
      status: laneFor(kinds),
      elapsed: elapsedByName.get(name) ?? 0,
      eventCount: kinds.length,
    });
  }
  return result;
}
