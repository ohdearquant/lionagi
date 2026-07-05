import type { SignalEvent } from "./api";

// ── Types ─────────────────────────────────────────────────────────────────────

export type OperationStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
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
  NodeCompleted: "succeeded",
  NodeFailed: "failed",
  NodeEscalated: "escalated",
};

export function laneFor(kinds: string[]): OperationStatus {
  let state: OperationStatus = "queued";
  let inTerminal = false;
  for (const k of kinds) {
    const newState = KIND_TO_STATE[k];
    if (!newState) continue;
    if (inTerminal && newState !== "queued" && newState !== "running") continue;
    state = newState;
    inTerminal = TERMINAL.has(state);
  }
  return state;
}

// ── Graph builder ─────────────────────────────────────────────────────────────

export function buildOperationGraph(events: SignalEvent[]): OperationGraphState {
  const order: string[] = [];
  const kindsByOp = new Map<string, string[]>();
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

    kindsByOp.get(ev.op_id)!.push(ev.kind);

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
      const causeOpId = payload.cause_op_id;
      if (typeof causeOpId === "string" && causeOpId) {
        causeOpIdByOp.set(ev.op_id, causeOpId);
        const edgeKey = `${causeOpId}→${ev.op_id}`;
        edgeSet.add(edgeKey);
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

  const edges = Array.from(edgeSet).map((key) => {
    const [source, target] = key.split("→");
    return { source: source!, target: target! };
  });

  return { nodes, edges };
}
