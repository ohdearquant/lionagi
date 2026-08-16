import type { SignalEvent } from "./api";
import type { NodeActivitySnapshot } from "./nodeActivity";
import { advanceLane, operationStatusForSignal, transitiveReduce } from "./operationGraph";
import type {
  LaneSignal,
  NodeSignalStatus,
  OperationEdge,
  OperationGraphState,
  OperationStatus,
} from "./operationGraph";
import { deriveVerdict } from "./runStatus";
import type { Verdict } from "./runStatus";

export const DEFAULT_SIGNAL_WINDOW_CAP = 2_000;

export interface SignalLaneSummary {
  op_id: string;
  lane: OperationStatus;
  count: number;
}

export interface SignalGateOutcome {
  verdict: Verdict;
  major: number;
  minor: number;
  hasFindings: boolean;
}

export interface SignalProjectionSnapshot {
  events: SignalEvent[];
  totalCount: number;
  hasOlder: boolean;
  oldestRetainedSeq: number | null;
  gateOutcome: SignalGateOutcome | null;
  laneSummaries: SignalLaneSummary[];
  nodeStatuses: Map<string, NodeSignalStatus>;
  nodeActivity: Map<string, NodeActivitySnapshot>;
  operationGraph: OperationGraphState;
}

interface OperationAggregate {
  name: string;
  status: OperationStatus;
  causeOpId: string | null;
  elapsed: number;
  firstTs: number;
  lastTs: number;
  eventCount: number;
}

interface LaneAggregate {
  status: OperationStatus;
  count: number;
}

interface ActivityAggregate extends NodeActivitySnapshot {
  firstOrder: number;
  activityOrder: number;
  textOrder: number;
  counterOrder: number;
}

const TOOL_KINDS = new Set(["ToolCallStarted", "ToolCallCompleted"]);
const STREAM_KINDS = new Set(["AssistantDelta", "MessageDelta"]);
const BLOCKING_FINDING_SEVERITIES = new Set(["critical", "high"]);

function laneSignal(event: SignalEvent): LaneSignal {
  const route = event.payload?.route;
  return typeof route === "string" ? { kind: event.kind, route } : event.kind;
}

function emptyActivity(): ActivityAggregate {
  return {
    activity: null,
    activityDetail: null,
    lastText: null,
    counter: null,
    lastEventAt: null,
    liveSignalAt: null,
    eventCount: 0,
    firstOrder: Number.POSITIVE_INFINITY,
    activityOrder: -1,
    textOrder: -1,
    counterOrder: -1,
  };
}

function firstString(payload: Record<string, unknown> | undefined, keys: string[]): string | null {
  if (!payload) return null;
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value) return value;
  }
  return null;
}

function firstNumber(payload: Record<string, unknown> | undefined, keys: string[]): number | null {
  if (!payload) return null;
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function foldActivity(target: ActivityAggregate, event: SignalEvent, order: number): void {
  target.firstOrder = Math.min(target.firstOrder, order);
  target.eventCount += 1;
  if (target.lastEventAt === null || event.ts > target.lastEventAt) {
    target.lastEventAt = event.ts;
  }

  const text = firstString(event.payload, ["text", "assistant_text", "delta"]);
  if (text) {
    target.lastText = text;
    target.textOrder = order;
  }

  const count = firstNumber(event.payload, ["token_count", "tokens", "event_count"]);
  if (count !== null) {
    target.counter = count;
    target.counterOrder = order;
  }

  const toolName = firstString(event.payload, ["tool_name", "tool"]);
  if (
    TOOL_KINDS.has(event.kind) ||
    STREAM_KINDS.has(event.kind) ||
    text ||
    toolName ||
    count !== null
  ) {
    if (target.liveSignalAt === null || event.ts > target.liveSignalAt) {
      target.liveSignalAt = event.ts;
    }
  }

  if (TOOL_KINDS.has(event.kind) || toolName) {
    target.activity = "tool";
    target.activityDetail = toolName;
    target.activityOrder = order;
  } else if (STREAM_KINDS.has(event.kind) || text) {
    target.activity = "streaming";
    target.activityDetail = null;
    target.activityOrder = order;
  } else if (event.kind === "NodeStarted") {
    target.activity = "thinking";
    target.activityDetail = null;
    target.activityOrder = order;
  } else if (event.kind === "NodeQueued") {
    target.activity = "waiting";
    target.activityDetail = null;
    target.activityOrder = order;
  }
}

function mergeActivity(target: ActivityAggregate, source: ActivityAggregate): void {
  target.firstOrder = Math.min(target.firstOrder, source.firstOrder);
  target.eventCount += source.eventCount;
  if (
    source.lastEventAt !== null &&
    (target.lastEventAt === null || source.lastEventAt > target.lastEventAt)
  ) {
    target.lastEventAt = source.lastEventAt;
  }
  if (
    source.liveSignalAt !== null &&
    (target.liveSignalAt === null || source.liveSignalAt > target.liveSignalAt)
  ) {
    target.liveSignalAt = source.liveSignalAt;
  }
  if (source.textOrder > target.textOrder) {
    target.lastText = source.lastText;
    target.textOrder = source.textOrder;
  }
  if (source.counterOrder > target.counterOrder) {
    target.counter = source.counter;
    target.counterOrder = source.counterOrder;
  }
  if (source.activityOrder > target.activityOrder) {
    target.activity = source.activity;
    target.activityDetail = source.activityDetail;
    target.activityOrder = source.activityOrder;
  }
}

function activitySnapshot(source: ActivityAggregate): NodeActivitySnapshot {
  return {
    activity: source.activity,
    activityDetail: source.activityDetail,
    lastText: source.lastText,
    counter: source.counter,
    lastEventAt: source.lastEventAt,
    liveSignalAt: source.liveSignalAt,
    eventCount: source.eventCount,
  };
}

export function gateOutcomeFromEvent(event: SignalEvent): SignalGateOutcome | null {
  if (event.kind !== "StructuredOutput") return null;
  const data = event.payload?.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const value = data as Record<string, unknown>;
  if (typeof value.gate_verdict === "string" && value.gate_verdict.length > 0) {
    const findings = Array.isArray(value.findings) ? value.findings : [];
    let major = 0;
    let minor = 0;
    for (const finding of findings) {
      const severity =
        finding && typeof finding === "object"
          ? (finding as Record<string, unknown>).severity
          : null;
      if (typeof severity === "string" && BLOCKING_FINDING_SEVERITIES.has(severity)) major += 1;
      else minor += 1;
    }
    return {
      verdict: deriveVerdict(value.gate_verdict),
      major,
      minor,
      hasFindings: true,
    };
  }
  if (typeof value.gate_passed === "boolean") {
    return {
      verdict: value.gate_passed ? "approve" : "reject",
      major: 0,
      minor: 0,
      hasFindings: false,
    };
  }
  return null;
}

/**
 * Incremental, bounded client-side signal index.
 *
 * Raw payload rows live in a fixed-size ring.  Graph, status, activity, gate,
 * and lane projections keep compact per-operation/per-name aggregates, so
 * their memory and update cost follow the execution graph rather than the
 * lifetime signal count.
 */
export class SignalProjection {
  private readonly cap: number;
  private readonly slots: Array<SignalEvent | undefined>;
  private start = 0;
  private length = 0;
  private accepted = 0;
  private highestSeq = 0;
  private readonly retainedIds = new Set<string>();

  private readonly operationOrder: string[] = [];
  private readonly operations = new Map<string, OperationAggregate>();
  private readonly plainEdges = new Set<string>();
  private readonly independentSpawnOrigins = new Map<string, { source: string; target: string }>();
  private readonly higherTierEscalations = new Set<string>();

  private readonly statusByName = new Map<string, NodeSignalStatus>();
  private readonly activityByName = new Map<string, ActivityAggregate>();
  private readonly unresolvedActivityByOp = new Map<string, ActivityAggregate>();
  private readonly nameByOp = new Map<string, string>();

  private readonly laneOrder: string[] = [];
  private readonly lanes = new Map<string, LaneAggregate>();
  private latestGateOutcome: SignalGateOutcome | null = null;

  constructor(cap: number = DEFAULT_SIGNAL_WINDOW_CAP) {
    if (!Number.isInteger(cap) || cap <= 0) {
      throw new RangeError("signal window cap must be a positive integer");
    }
    this.cap = cap;
    this.slots = new Array<SignalEvent | undefined>(cap);
  }

  append(event: SignalEvent): boolean {
    // The persisted stream is strictly ordered by its unique sequence.  This
    // rejects reconnect replay in O(1), including duplicates that have aged
    // out of the raw ring, while the id set catches malformed same-window
    // rows that reuse an id with a different sequence.
    if (event.seq < this.highestSeq || this.retainedIds.has(event.id)) return false;

    this.highestSeq = Math.max(this.highestSeq, event.seq);
    this.accepted += 1;
    this.appendRaw(event);
    this.appendLane(event);
    this.appendOperation(event);
    this.appendStatus(event);
    this.appendActivity(event);
    const gateOutcome = gateOutcomeFromEvent(event);
    if (gateOutcome) this.latestGateOutcome = gateOutcome;
    return true;
  }

  private appendRaw(event: SignalEvent): void {
    if (this.length < this.cap) {
      const index = (this.start + this.length) % this.cap;
      this.slots[index] = event;
      this.length += 1;
    } else {
      const evicted = this.slots[this.start];
      if (evicted) this.retainedIds.delete(evicted.id);
      this.slots[this.start] = event;
      this.start = (this.start + 1) % this.cap;
    }
    this.retainedIds.add(event.id);
  }

  private appendLane(event: SignalEvent): void {
    if (!event.op_id) return;
    let lane = this.lanes.get(event.op_id);
    if (!lane) {
      lane = { status: "queued", count: 0 };
      this.lanes.set(event.op_id, lane);
      this.laneOrder.push(event.op_id);
    }
    lane.count += 1;
    lane.status = advanceLane(lane.status, laneSignal(event));
  }

  private appendOperation(event: SignalEvent): void {
    if (!event.op_id) return;

    if (event.kind === "NodeSpawned") {
      const parentId = event.payload?.parent_id;
      if (
        event.payload?.independent === true &&
        typeof parentId === "string" &&
        parentId &&
        parentId !== event.op_id
      ) {
        const key = `${parentId}→${event.op_id}`;
        this.independentSpawnOrigins.set(key, { source: parentId, target: event.op_id });
      }
      return;
    }

    if (!operationStatusForSignal(event.kind)) return;
    if (event.kind === "NodeEscalated" && event.payload?.route === "higher_tier") {
      this.higherTierEscalations.add(event.op_id);
    }

    let aggregate = this.operations.get(event.op_id);
    if (!aggregate) {
      aggregate = {
        name: "",
        status: "queued",
        causeOpId: null,
        elapsed: 0,
        firstTs: event.ts,
        lastTs: event.ts,
        eventCount: 0,
      };
      this.operations.set(event.op_id, aggregate);
      this.operationOrder.push(event.op_id);
    }

    aggregate.status = advanceLane(aggregate.status, laneSignal(event));
    aggregate.eventCount += 1;
    aggregate.firstTs = Math.min(aggregate.firstTs, event.ts);
    aggregate.lastTs = Math.max(aggregate.lastTs, event.ts);

    const payload = event.payload;
    const name = payload?.name;
    if (!aggregate.name && typeof name === "string" && name) aggregate.name = name;
    const elapsed = payload?.elapsed;
    if (typeof elapsed === "number" && elapsed > aggregate.elapsed) aggregate.elapsed = elapsed;

    const causeOpId = payload?.cause_op_id;
    const parentId = payload?.parent_id;
    const primaryCause =
      (typeof causeOpId === "string" && causeOpId) ||
      (typeof parentId === "string" && parentId) ||
      null;
    if (primaryCause) {
      if (!aggregate.causeOpId) aggregate.causeOpId = primaryCause;
      if (primaryCause !== event.op_id) this.plainEdges.add(`${primaryCause}→${event.op_id}`);
    }

    const dependsOn = payload?.depends_on;
    if (Array.isArray(dependsOn)) {
      for (const dependency of dependsOn) {
        if (typeof dependency === "string" && dependency && dependency !== event.op_id) {
          this.plainEdges.add(`${dependency}→${event.op_id}`);
        }
      }
    }
  }

  private appendStatus(event: SignalEvent): void {
    if (!operationStatusForSignal(event.kind)) return;
    const name = typeof event.payload?.name === "string" ? event.payload.name : "";
    if (!name) return;
    const previous = this.statusByName.get(name) ?? {
      status: "queued" as OperationStatus,
      elapsed: 0,
      eventCount: 0,
    };
    const elapsed = event.payload?.elapsed;
    this.statusByName.set(name, {
      status: advanceLane(previous.status, laneSignal(event)),
      elapsed:
        typeof elapsed === "number" && elapsed > previous.elapsed ? elapsed : previous.elapsed,
      eventCount: previous.eventCount + 1,
    });
  }

  private appendActivity(event: SignalEvent): void {
    const directName = typeof event.payload?.name === "string" ? event.payload.name : "";
    if (directName && event.op_id && !this.nameByOp.has(event.op_id)) {
      this.nameByOp.set(event.op_id, directName);
      const pending = this.unresolvedActivityByOp.get(event.op_id);
      if (pending) {
        const target = this.activityByName.get(directName) ?? emptyActivity();
        mergeActivity(target, pending);
        this.activityByName.set(directName, target);
        this.unresolvedActivityByOp.delete(event.op_id);
      }
    }

    const resolvedName = directName || this.nameByOp.get(event.op_id) || "";
    if (resolvedName) {
      const target = this.activityByName.get(resolvedName) ?? emptyActivity();
      foldActivity(target, event, this.accepted);
      this.activityByName.set(resolvedName, target);
    } else if (event.op_id) {
      const target = this.unresolvedActivityByOp.get(event.op_id) ?? emptyActivity();
      foldActivity(target, event, this.accepted);
      this.unresolvedActivityByOp.set(event.op_id, target);
    }
  }

  get events(): SignalEvent[] {
    const result: SignalEvent[] = [];
    for (let offset = 0; offset < this.length; offset += 1) {
      const event = this.slots[(this.start + offset) % this.cap];
      if (event) result.push(event);
    }
    return result;
  }

  get totalCount(): number {
    return this.accepted;
  }

  get hasOlder(): boolean {
    return this.accepted > this.length;
  }

  get oldestRetainedSeq(): number | null {
    return this.events[0]?.seq ?? null;
  }

  get gateOutcome(): SignalGateOutcome | null {
    return this.latestGateOutcome;
  }

  get laneSummaries(): SignalLaneSummary[] {
    return this.laneOrder.map((op_id) => {
      const aggregate = this.lanes.get(op_id)!;
      return { op_id, lane: aggregate.status, count: aggregate.count };
    });
  }

  get nodeStatuses(): Map<string, NodeSignalStatus> {
    return new Map(Array.from(this.statusByName, ([name, value]) => [name, { ...value }] as const));
  }

  get nodeActivity(): Map<string, NodeActivitySnapshot> {
    return new Map(
      Array.from(this.activityByName)
        .sort(([, left], [, right]) => left.firstOrder - right.firstOrder)
        .map(([name, value]) => [name, activitySnapshot(value)] as const),
    );
  }

  get operationGraph(): OperationGraphState {
    const edges = new Set(this.plainEdges);
    const causeByOp = new Map(
      Array.from(this.operations, ([opId, aggregate]) => [opId, aggregate.causeOpId] as const),
    );
    const names = new Map(
      Array.from(this.operations, ([opId, aggregate]) => [opId, aggregate.name] as const),
    );
    const continuations: OperationEdge[] = [];

    for (const [key, origin] of this.independentSpawnOrigins) {
      if (!this.higherTierEscalations.has(origin.source)) continue;
      edges.delete(key);
      causeByOp.set(origin.target, null);
      continuations.push({ ...origin, continuation: true });

      const originName = names.get(origin.source);
      const childName = names.get(origin.target);
      const originHasReadableName = Boolean(originName) && originName !== origin.source.slice(0, 8);
      const childHasFallbackName = !childName || childName === origin.target.slice(0, 8);
      if (originHasReadableName && childHasFallbackName) {
        names.set(origin.target, `${originName} escalation retry`);
      }
    }

    const nodes = this.operationOrder.map((opId) => {
      const aggregate = this.operations.get(opId)!;
      return {
        opId,
        name: names.get(opId) ?? "",
        status: aggregate.status,
        causeOpId: causeByOp.get(opId) ?? null,
        elapsed: aggregate.elapsed,
        firstTs: aggregate.firstTs,
        lastTs: aggregate.lastTs,
        eventCount: aggregate.eventCount,
      };
    });

    const plain = Array.from(edges, (key) => {
      const [source, target] = key.split("→");
      return { source: source!, target: target! };
    });
    return { nodes, edges: [...transitiveReduce(plain), ...continuations] };
  }

  snapshot(): SignalProjectionSnapshot {
    return {
      events: this.events,
      totalCount: this.totalCount,
      hasOlder: this.hasOlder,
      oldestRetainedSeq: this.oldestRetainedSeq,
      gateOutcome: this.gateOutcome,
      laneSummaries: this.laneSummaries,
      nodeStatuses: this.nodeStatuses,
      nodeActivity: this.nodeActivity,
      operationGraph: this.operationGraph,
    };
  }
}
