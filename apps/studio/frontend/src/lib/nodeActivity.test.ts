import { describe, expect, it } from "vitest";
import { deriveNodeActivity, isStalled, pulseDurationMs, STALL_TIMEOUT_MS } from "./nodeActivity";
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
