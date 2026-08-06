import type { SignalEvent } from "./api";

// ── Types ─────────────────────────────────────────────────────────────────────

export type OperationStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "paused"
  | "succeeded"
  | "failed"
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

export interface OperationGraphState {
  nodes: OperationNode[];
  edges: { source: string; target: string }[];
}

// ── Status projection ─────────────────────────────────────────────────────────

const TERMINAL = new Set<OperationStatus>(["succeeded", "failed", "escalated"]);

const KIND_TO_STATE: Record<string, OperationStatus | undefined> = {
  NodeQueued: "queued",
  NodeStarted: "running",
  NodeAwaitingApproval: "awaiting_approval",
  NodePaused: "paused",
  NodeCompleted: "succeeded",
  NodeFailed: "failed",
  NodeEscalated: "escalated",
};

// A lane-projection input: either a bare kind string (back-compat; an
// unaccompanied "NodeEscalated" with no route info still projects to
// "escalated", matching an EscalationRequest-less signal on the backend) or
// a {kind, route} pair carrying the NodeEscalated route so a soft ("fyi")
// help signal (route="notify") can be told apart from a real escalation.
export type LaneSignal = string | { kind: string; route?: string };

export function laneFor(kinds: LaneSignal[]): OperationStatus {
  let state: OperationStatus = "queued";
  let inTerminal = false;
  for (const entry of kinds) {
    const k = typeof entry === "string" ? entry : entry.kind;
    const route = typeof entry === "string" ? undefined : entry.route;
    // Soft ("fyi") help signal: NodeEscalated fires for observability but
    // the emitting node keeps working toward its own terminal state — must
    // not get pinned into the terminal "escalated" lane. Mirrors
    // _signal_to_state returning None for urgency="fyi" in
    // lionagi/session/signal.py.
    if (k === "NodeEscalated" && route === "notify") continue;
    const newState = KIND_TO_STATE[k];
    if (!newState) continue;
    if (inTerminal && newState !== "queued" && newState !== "running") continue;
    state = newState;
    inTerminal = TERMINAL.has(state);
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
// through OTHER edges that themselves survive as real dependencies. Two
// guards distinguish this from transitiveReduce above:
//   1. Whole-graph cycle detection — a cyclic graph is returned unchanged
//      rather than reduced around the cycle.
//   2. Semantically rich edges (see defaultIsRich) are never dropped, even
//      when reachable via another path — they carry author intent, not
//      structural redundancy.
// Never mutates or re-persists the input; this only affects what is
// rendered.
export function transitiveReduceDisplay<E extends { source: string; target: string }>(
  edges: E[],
  options?: { isRich?: (e: E) => boolean },
): { kept: E[]; hidden: E[] } {
  if (edges.length === 0) return { kept: [], hidden: [] };
  if (hasCycle(edges)) return { kept: edges, hidden: [] };

  const isRich = options?.isRich ?? defaultIsRich;
  const rich: E[] = [];
  const plain: E[] = [];
  for (const e of edges) {
    (isRich(e) ? rich : plain).push(e);
  }

  const outEdges = new Map<string, E[]>();
  for (const e of edges) {
    (outEdges.get(e.source) ?? outEdges.set(e.source, []).get(e.source)!).push(e);
  }

  const reachableCache = new Map<string, Set<string>>();
  const reachableFrom = (node: string): Set<string> => {
    const cached = reachableCache.get(node);
    if (cached) return cached;
    const result = new Set<string>();
    reachableCache.set(node, result); // seed before recursing; the graph is acyclic here
    for (const e of outEdges.get(node) ?? []) {
      result.add(e.target);
      for (const r of reachableFrom(e.target)) result.add(r);
    }
    return result;
  };

  const hidden: E[] = [];
  const survivingPlain = plain.filter((e) => {
    for (const alt of outEdges.get(e.source) ?? []) {
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

  for (const ev of events) {
    if (!ev.op_id) continue;
    if (!KIND_TO_STATE[ev.kind]) continue;

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

  const edges = transitiveReduce(
    Array.from(edgeSet).map((key) => {
      const [source, target] = key.split("→");
      return { source: source!, target: target! };
    }),
  );

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
