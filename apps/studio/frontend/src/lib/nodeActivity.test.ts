import { describe, expect, it } from "vitest";
import {
  buildNodeActivityByName,
  deriveNodeActivity,
  isStalled,
  pulseDurationMs,
  STALL_TIMEOUT_MS,
} from "./nodeActivity";
import type { SignalEvent } from "./api";

function ev(kind: string, ts: number, payload: Record<string, unknown> = {}): SignalEvent {
  return { id: `${kind}-${ts}`, session_id: "s", seq: ts, kind, op_id: "op1", ts, payload };
}

describe("deriveNodeActivity", () => {
  it("reports nothing for an empty event log", () => {
    const snap = deriveNodeActivity([]);
    expect(snap).toEqual({
      activity: null,
      activityDetail: null,
      lastText: null,
      counter: null,
      lastEventAt: null,
      liveSignalAt: null,
      eventCount: 0,
    });
  });

  it("reads 'thinking' from a bare NodeStarted with no richer payload", () => {
    const snap = deriveNodeActivity([ev("NodeStarted", 100, { name: "a" })]);
    expect(snap.activity).toBe("thinking");
    expect(snap.lastText).toBeNull();
    expect(snap.lastEventAt).toBe(100);
  });

  it("reads 'waiting' from a bare NodeQueued", () => {
    const snap = deriveNodeActivity([ev("NodeQueued", 50)]);
    expect(snap.activity).toBe("waiting");
  });

  it("picks up streaming text from any kind that carries a text-ish field", () => {
    const snap = deriveNodeActivity([
      ev("NodeStarted", 1),
      ev("NodeStarted", 2, { text: "hello" }),
    ]);
    expect(snap.activity).toBe("streaming");
    expect(snap.lastText).toBe("hello");
  });

  it("prefers the tool activity when a tool name is present", () => {
    const snap = deriveNodeActivity([ev("NodeStarted", 1, { tool_name: "read_file" })]);
    expect(snap.activity).toBe("tool");
    expect(snap.activityDetail).toBe("read_file");
  });

  it("last-write-wins on text and counter across the event log, in the order given", () => {
    const snap = deriveNodeActivity([
      ev("NodeStarted", 1, { text: "first", token_count: 5 }),
      ev("NodeStarted", 2, { text: "second", token_count: 12 }),
    ]);
    expect(snap.lastText).toBe("second");
    expect(snap.counter).toBe(12);
    expect(snap.eventCount).toBe(2);
  });

  it("tracks lastEventAt as the max timestamp regardless of array order", () => {
    const snap = deriveNodeActivity([ev("NodeStarted", 500), ev("NodeQueued", 10)]);
    expect(snap.lastEventAt).toBe(500);
  });
});

// The shape today's backend actually produces, taken from a real run: a node
// emits NodeStarted, then nothing at all for the 32-39 seconds it works, then
// NodeCompleted. Reading event silence as a stall turns every live node on the
// canvas into a stalled one for roughly two thirds of its life, and the longer
// the op the worse it reads. `liveSignalAt` is what separates "never reported
// work" from "reported work and stopped".
describe("liveSignalAt separates silence-by-design from a dead stream", () => {
  it("stays null across a lifecycle-only node, however long it runs", () => {
    const snap = deriveNodeActivity([
      ev("NodeQueued", 0, { name: "writer" }),
      ev("NodeStarted", 10, { name: "writer" }),
      ev("NodeCompleted", 35_510, { name: "writer" }),
    ]);
    expect(snap.lastEventAt).toBe(35_510);
    expect(snap.liveSignalAt).toBeNull();
  });

  it("does not call a working lifecycle-only node stalled", () => {
    const snap = deriveNodeActivity([
      ev("NodeQueued", 0, { name: "writer" }),
      ev("NodeStarted", 10, { name: "writer" }),
    ]);
    // Well past the timeout, which is the normal case for a real op.
    const now = 10 + STALL_TIMEOUT_MS * 3;
    expect(isStalled(snap.liveSignalAt, now)).toBe(false);
    // And the reading this replaces, so the test states what it prevents.
    expect(isStalled(snap.lastEventAt, now)).toBe(true);
  });

  it("still catches a node whose real stream died", () => {
    // The failure the stall reading exists for: work WAS being reported, then
    // it stopped. Without this arm the fix above would be indistinguishable
    // from deleting the feature.
    const snap = deriveNodeActivity([
      ev("NodeStarted", 10, { name: "writer" }),
      ev("AssistantDelta", 500, { name: "writer", text: "partial" }),
    ]);
    expect(snap.liveSignalAt).toBe(500);
    expect(isStalled(snap.liveSignalAt, 500 + STALL_TIMEOUT_MS + 1)).toBe(true);
    expect(isStalled(snap.liveSignalAt, 500 + STALL_TIMEOUT_MS)).toBe(false);
  });

  it("advances on a tool call and on a counter, not just on text", () => {
    expect(
      deriveNodeActivity([ev("ToolCallStarted", 42, { name: "w", tool_name: "grep" })]).liveSignalAt,
    ).toBe(42);
    expect(deriveNodeActivity([ev("Whatever", 77, { name: "w", token_count: 12 })]).liveSignalAt).toBe(
      77,
    );
  });
});

describe("isStalled", () => {
  it("is never stalled with no event yet", () => {
    expect(isStalled(null, 999_999)).toBe(false);
  });

  it("is not stalled right up to the timeout boundary", () => {
    expect(isStalled(1000, 1000 + STALL_TIMEOUT_MS)).toBe(false);
  });

  it("is stalled the instant the timeout is exceeded", () => {
    expect(isStalled(1000, 1000 + STALL_TIMEOUT_MS + 1)).toBe(true);
  });
});

describe("pulseDurationMs", () => {
  it("falls back to the default duration with no events in the window", () => {
    expect(pulseDurationMs(0)).toBe(1500);
  });

  it("speeds up (shorter duration) as the event rate rises", () => {
    const slow = pulseDurationMs(1, 5000); // 0.2/s -> clamped floor
    const fast = pulseDurationMs(20, 5000); // 4/s -> clamped ceiling
    expect(fast).toBeLessThan(slow);
  });

  it("never goes below the compositor-friendly floor or above the perceptible ceiling", () => {
    for (const n of [0, 1, 5, 100, 10_000]) {
      const ms = pulseDurationMs(n);
      expect(ms).toBeGreaterThanOrEqual(700);
      expect(ms).toBeLessThanOrEqual(2400);
    }
  });
});

// The correlation is the part that fails silently: keying on op_id resolves
// nothing against a planned graph and yields an empty snapshot per node, which
// is indistinguishable from a run that has not emitted anything yet.
function named(
  name: string,
  opId: string,
  kind: string,
  ts: number,
  extra: Record<string, unknown> = {},
): SignalEvent {
  return {
    id: `${opId}-${kind}-${ts}`,
    session_id: "s",
    seq: ts,
    kind,
    op_id: opId,
    ts,
    payload: { name, ...extra },
  };
}

describe("buildNodeActivityByName", () => {
  it("buckets by the authored step name, not by the runtime op id", () => {
    // Same authored step, two different runtime ops (a retry, or a node the
    // engine re-created). Keyed by op_id this reads as two unrelated nodes,
    // neither of which matches anything the planned graph draws.
    const by = buildNodeActivityByName([
      named("plan", "uuid-a", "NodeStarted", 1),
      named("plan", "uuid-b", "ToolCallStarted", 2, { tool_name: "grep" }),
    ]);

    expect([...by.keys()]).toEqual(["plan"]);
    expect(by.get("plan")!.eventCount).toBe(2);
    expect(by.get("plan")!.activity).toBe("tool");
  });

  it("keeps separate authored steps separate", () => {
    const by = buildNodeActivityByName([
      named("plan", "uuid-a", "NodeStarted", 1),
      named("build", "uuid-b", "NodeQueued", 2),
    ]);

    expect(by.get("plan")!.activity).toBe("thinking");
    expect(by.get("build")!.activity).toBe("waiting");
  });

  it("places an event that carries only an op id, using the name that op already announced", () => {
    // The shape a richer emitter will have: the lifecycle signal names the
    // step, the interesting one does not repeat it.
    const bare: SignalEvent = {
      id: "bare",
      session_id: "s",
      seq: 2,
      kind: "ToolCallStarted",
      op_id: "uuid-a",
      ts: 2,
      payload: { tool_name: "rg" },
    };
    const by = buildNodeActivityByName([named("plan", "uuid-a", "NodeStarted", 1), bare]);

    expect(by.get("plan")!.eventCount).toBe(2);
    expect(by.get("plan")!.activityDetail).toBe("rg");
  });

  it("drops an event it cannot place rather than filing it under a guess", () => {
    const orphan: SignalEvent = {
      id: "orphan",
      session_id: "s",
      seq: 1,
      kind: "ToolCallStarted",
      op_id: "uuid-unknown",
      ts: 1,
      payload: { tool_name: "rg" },
    };
    const by = buildNodeActivityByName([named("plan", "uuid-a", "NodeStarted", 1), orphan]);

    expect([...by.keys()]).toEqual(["plan"]);
    expect(by.get("plan")!.eventCount).toBe(1);
  });

  it("returns an empty map for an empty stream", () => {
    expect(buildNodeActivityByName([]).size).toBe(0);
  });
});
