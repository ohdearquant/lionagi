// Pure, framework-free derivation of a node's live-activity snapshot (ADR-0113
// D3/row 6) from the raw signal-event stream StepNode's caller reads
// (`streamSignals` in ./api). Kept separate from StepNode so the folding logic
// is testable without mounting React, and separate from operationGraph.ts
// because that module derives LIFECYCLE (queued/running/...); this derives
// WHAT THE NODE IS DOING within the running state.
//
// Today's backend only emits the lifecycle signals in
// lionagi/session/signal.py (NodeQueued/Started/Completed/...), none of which
// carry assistant text, a tool name, or a token count. The fields below are
// read defensively from `payload` — a node fed only today's signals gets
// `activity: "thinking"` while running and everything else null, which is
// exactly what StepNode should show for a node it has no richer signal for.
// A future emitter can add payload fields (or new kinds) without any change
// on this side.
import type { SignalEvent } from "./api";

export type NodeActivityKind = "thinking" | "tool" | "streaming" | "waiting";

export interface NodeActivitySnapshot {
  activity: NodeActivityKind | null;
  activityDetail: string | null;
  lastText: string | null;
  counter: number | null;
  lastEventAt: number | null;
  eventCount: number;
}

const TOOL_KINDS = new Set(["ToolCallStarted", "ToolCallCompleted"]);
const STREAM_KINDS = new Set(["AssistantDelta", "MessageDelta"]);

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

// Folds events in whatever order they're given — last-write-wins per field —
// so a caller may pass either the full per-node event log or just the tail of
// it; both produce the same snapshot as long as order is preserved.
export function deriveNodeActivity(events: readonly SignalEvent[]): NodeActivitySnapshot {
  let lastText: string | null = null;
  let counter: number | null = null;
  let lastEventAt: number | null = null;
  let activity: NodeActivityKind | null = null;
  let activityDetail: string | null = null;

  for (const ev of events) {
    if (lastEventAt === null || ev.ts > lastEventAt) lastEventAt = ev.ts;

    const text = firstString(ev.payload, ["text", "assistant_text", "delta"]);
    if (text) lastText = text;

    const count = firstNumber(ev.payload, ["token_count", "tokens", "event_count"]);
    if (count !== null) counter = count;

    const toolName = firstString(ev.payload, ["tool_name", "tool"]);
    if (TOOL_KINDS.has(ev.kind) || toolName) {
      activity = "tool";
      activityDetail = toolName;
    } else if (STREAM_KINDS.has(ev.kind) || text) {
      activity = "streaming";
      activityDetail = null;
    } else if (ev.kind === "NodeStarted") {
      activity = "thinking";
      activityDetail = null;
    } else if (ev.kind === "NodeQueued") {
      activity = "waiting";
      activityDetail = null;
    }
  }

  return { activity, activityDetail, lastText, counter, lastEventAt, eventCount: events.length };
}

// Correlates the raw event stream against a PLANNED graph, whose node ids are
// authored step names — so events bucket by `payload.name`, never by `op_id`
// (a runtime UUID the planned graph knows nothing about; see
// operationGraph.ts's buildNodeStatusesByName, which correlates the same way
// for lifecycle status). Keying on op_id here would resolve nothing and
// produce an empty snapshot for every node, which reads as "no signals yet"
// rather than as the miscorrelation it is.
//
// Unlike the lifecycle path this does not filter by kind: every event for a
// node feeds the activity fold, because the richer kinds a future emitter adds
// are exactly the ones worth showing. Such an event may carry only `op_id`, so
// the name learned from that op's Node* events is remembered and used to place
// it. An event whose op has never carried a name is dropped rather than filed
// under a guess.
export function buildNodeActivityByName(
  events: readonly SignalEvent[],
): Map<string, NodeActivitySnapshot> {
  const nameByOp = new Map<string, string>();
  for (const ev of events) {
    const name = ev.payload && typeof ev.payload.name === "string" ? ev.payload.name : "";
    if (name && ev.op_id && !nameByOp.has(ev.op_id)) nameByOp.set(ev.op_id, name);
  }

  const eventsByName = new Map<string, SignalEvent[]>();
  for (const ev of events) {
    const direct = ev.payload && typeof ev.payload.name === "string" ? ev.payload.name : "";
    const name = direct || nameByOp.get(ev.op_id) || "";
    if (!name) continue;
    const bucket = eventsByName.get(name);
    if (bucket) bucket.push(ev);
    else eventsByName.set(name, [ev]);
  }

  const result = new Map<string, NodeActivitySnapshot>();
  for (const [name, bucket] of eventsByName) {
    result.set(name, deriveNodeActivity(bucket));
  }
  return result;
}

// A node "stalls" once this long passes with no fresh event while it is still
// reporting itself as running — the failure mode ADR-0113's Consequences
// section names explicitly: a node pulsing forever after its event stream
// dies asserts liveness it no longer has.
export const STALL_TIMEOUT_MS = 12_000;

export function isStalled(
  lastEventAt: number | null,
  now: number,
  timeoutMs: number = STALL_TIMEOUT_MS,
): boolean {
  if (lastEventAt === null) return false;
  return now - lastEventAt > timeoutMs;
}

// Pulse speed is derived from the event rate, not fixed — a node emitting
// many events per second reads as busier than one emitting one every few
// seconds. Bucketed and clamped so a burst or a lull can't animate faster
// than the compositor should be asked to run, or slower than a human
// perceives as "still moving".
const MIN_PULSE_MS = 700;
const MAX_PULSE_MS = 2400;
const DEFAULT_PULSE_MS = 1500;

export function pulseDurationMs(eventsInWindow: number, windowMs = 5000): number {
  if (eventsInWindow <= 0) return DEFAULT_PULSE_MS;
  const ratePerSec = eventsInWindow / (windowMs / 1000);
  const ms = 1000 / Math.min(Math.max(ratePerSec, 0.4), 4);
  return Math.min(Math.max(ms, MIN_PULSE_MS), MAX_PULSE_MS);
}
