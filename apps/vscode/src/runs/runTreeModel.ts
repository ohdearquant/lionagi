// Pure reducer — no vscode import so this module stays unit-testable.
import type { NodeLifecycleState, SignalRow } from "../api/types.js";

export type { NodeLifecycleState };

export interface RunTreeNode {
  opId: string;
  name: string;
  parentId: string | null;
  dependsOn: string[];
  state: NodeLifecycleState;
  elapsed: number;
  instruction?: string;
  assignee?: string;
}

export interface RunUsage {
  inputTokens: number;
  outputTokens: number;
  totalCostUsd: number;
  numTurns: number;
  durationMs: number;
}

export interface RunTreeNode_WithChildren extends RunTreeNode {
  children: RunTreeNode_WithChildren[];
}

export interface RunTreeState {
  nodes: Map<string, RunTreeNode>;
  order: string[];
  runState: "pending" | "running" | "succeeded" | "failed";
  usage: RunUsage | null;
}

export function createRunTreeState(): RunTreeState {
  return {
    nodes: new Map(),
    order: [],
    runState: "pending",
    usage: null,
  };
}

/** Terminal states that are sticky — a later signal only resets them on retry. */
const TERMINAL: ReadonlySet<NodeLifecycleState> = new Set([
  "succeeded",
  "failed",
  "escalated",
]);

/**
 * Apply a single signal row to the state in-place (ascending seq order).
 * Mirrors lane_for() in lionagi/session/signal.py lines 183-219.
 */
export function applySignalRow(state: RunTreeState, row: SignalRow): void {
  const kind = row.kind;

  if (kind === "RunStart") {
    state.runState = "running";
    return;
  }

  if (kind === "RunEnd") {
    state.runState = "succeeded";
    const p = row.payload;
    state.usage = {
      inputTokens: (p["input_tokens"] as number) ?? 0,
      outputTokens: (p["output_tokens"] as number) ?? 0,
      totalCostUsd: (p["total_cost_usd"] as number) ?? 0,
      numTurns: (p["num_turns"] as number) ?? 0,
      durationMs: (p["duration_ms"] as number) ?? 0,
    };
    return;
  }

  if (kind === "RunFailed") {
    state.runState = "failed";
    return;
  }

  // Node* signals — resolve op_id from payload first, fall back to row-level.
  const p = row.payload;
  const opId = (p["op_id"] as string | undefined) ?? row.op_id;
  if (!opId) {
    return;
  }

  // Determine the new lifecycle state for this signal kind.
  let newState: NodeLifecycleState | null = null;
  switch (kind) {
    case "NodeQueued":
      newState = "queued";
      break;
    case "NodeStarted":
      newState = "running";
      break;
    case "NodeAwaitingApproval":
      newState = "awaiting_approval";
      break;
    case "NodeCompleted":
      newState = "succeeded";
      break;
    case "NodeFailed":
      newState = "failed";
      break;
    case "NodeEscalated":
      newState = "escalated";
      break;
    case "NodeSpawned":
      // Not a lifecycle transition on its own; upsert with default state "queued"
      // so the node appears in the tree as soon as it is announced.
      newState = "queued";
      break;
    default:
      // MessageAdded, GateDenied, StructuredOutput — skip.
      return;
  }

  const existing = state.nodes.get(opId);

  // Sticky terminal: only reset if the new signal is queued/running (retry).
  if (
    existing &&
    TERMINAL.has(existing.state) &&
    newState !== "queued" &&
    newState !== "running"
  ) {
    // Still update mutable fields (name, elapsed, etc.) even if state is sticky.
    patchNode(existing, p, kind);
    return;
  }

  if (!existing) {
    // First time we see this op_id — add to order list.
    state.order.push(opId);
    const node: RunTreeNode = {
      opId,
      name: (p["name"] as string | undefined) ?? opId,
      parentId: (p["parent_id"] as string | null | undefined) ?? null,
      dependsOn: normalizeDependsOn(p["depends_on"]),
      state: newState,
      elapsed: (p["elapsed"] as number | undefined) ?? 0,
      instruction: p["instruction"] as string | undefined,
      assignee: p["assignee"] as string | undefined,
    };
    state.nodes.set(opId, node);
  } else {
    existing.state = newState;
    patchNode(existing, p, kind);
  }
}

function patchNode(
  node: RunTreeNode,
  p: Record<string, unknown>,
  kind: string
): void {
  if (typeof p["name"] === "string" && p["name"]) {
    node.name = p["name"];
  }
  if (p["parent_id"] !== undefined) {
    node.parentId = (p["parent_id"] as string | null) ?? null;
  }
  if (p["depends_on"] !== undefined) {
    node.dependsOn = normalizeDependsOn(p["depends_on"]);
  }
  if (typeof p["elapsed"] === "number") {
    node.elapsed = p["elapsed"];
  }
  if (typeof p["instruction"] === "string") {
    node.instruction = p["instruction"];
  }
  if (typeof p["assignee"] === "string") {
    node.assignee = p["assignee"];
  }
  // NodeSpawned carries independent/assignee fields that later signals may not.
  if (kind === "NodeSpawned") {
    if (typeof p["assignee"] === "string") {
      node.assignee = p["assignee"];
    }
  }
}

function normalizeDependsOn(v: unknown): string[] {
  if (Array.isArray(v)) {
    return (v as unknown[]).filter((x) => typeof x === "string") as string[];
  }
  if (typeof v === "string" && v) {
    return [v];
  }
  return [];
}

/**
 * Build a forest (array of root nodes with nested children) from the flat state.
 * Roots are nodes whose parentId is null/undefined or whose parent is not in nodes.
 * Children are attached in state.order order at each level.
 *
 * Handles the flat case gracefully: if nodes is empty, returns [].
 */
export function toForest(state: RunTreeState): RunTreeNode_WithChildren[] {
  if (state.nodes.size === 0) {
    return [];
  }

  // Build a map of opId → children list (in order).
  const childMap = new Map<string, RunTreeNode_WithChildren[]>();
  const roots: RunTreeNode_WithChildren[] = [];

  // First pass: wrap every node.
  const wrapped = new Map<string, RunTreeNode_WithChildren>();
  for (const opId of state.order) {
    const node = state.nodes.get(opId);
    if (!node) {
      continue;
    }
    const w: RunTreeNode_WithChildren = { ...node, children: [] };
    wrapped.set(opId, w);
    childMap.set(opId, w.children);
  }

  // Second pass: attach to parent or roots.
  for (const opId of state.order) {
    const w = wrapped.get(opId);
    if (!w) {
      continue;
    }
    const parentId = w.parentId;
    if (parentId && parentId !== w.opId && wrapped.has(parentId)) {
      childMap.get(parentId)!.push(w);
    } else {
      roots.push(w);
    }
  }

  return roots;
}
