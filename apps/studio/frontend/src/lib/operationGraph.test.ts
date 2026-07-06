import { describe, it, expect } from "vitest";
import { buildOperationGraph, laneFor } from "./operationGraph";
import type { SignalEvent } from "./api";

// ── Helpers ───────────────────────────────────────────────────────────────────

function ev(
  id: string,
  kind: string,
  op_id: string,
  payload: Record<string, unknown> = {},
  ts = 1000,
): SignalEvent {
  return { id, session_id: "s1", seq: 0, kind, op_id, ts, payload };
}

// ── laneFor — status projection ───────────────────────────────────────────────

describe("laneFor — status projection", () => {
  it("returns queued for empty kinds", () => {
    expect(laneFor([])).toBe("queued");
  });

  it("NodeQueued → queued", () => {
    expect(laneFor(["NodeQueued"])).toBe("queued");
  });

  it("NodeStarted → running", () => {
    expect(laneFor(["NodeStarted"])).toBe("running");
  });

  it("NodeCompleted → succeeded", () => {
    expect(laneFor(["NodeCompleted"])).toBe("succeeded");
  });

  it("NodeFailed → failed", () => {
    expect(laneFor(["NodeFailed"])).toBe("failed");
  });

  it("NodeEscalated → escalated", () => {
    expect(laneFor(["NodeEscalated"])).toBe("escalated");
  });

  it("NodeAwaitingApproval → awaiting_approval", () => {
    expect(laneFor(["NodeAwaitingApproval"])).toBe("awaiting_approval");
  });

  it("queued→started→completed is sticky terminal succeeded", () => {
    expect(laneFor(["NodeQueued", "NodeStarted", "NodeCompleted"])).toBe("succeeded");
  });

  it("terminal succeeded allows queued to reset (re-queue semantics)", () => {
    // queued resets from terminal — original laneFor allows queued and running through
    expect(laneFor(["NodeCompleted", "NodeQueued"])).toBe("queued");
  });

  it("terminal succeeded allows running to reset (re-queue semantics)", () => {
    // running resets from terminal — matches original EventsSection laneFor
    expect(laneFor(["NodeCompleted", "NodeStarted"])).toBe("running");
  });

  it("terminal failed allows running to reset", () => {
    expect(laneFor(["NodeFailed", "NodeStarted"])).toBe("running");
  });

  it("terminal succeeded ignores a second terminal state (non-queued/running)", () => {
    // After succeeded, NodeFailed is skipped — only queued/running can reset
    expect(laneFor(["NodeCompleted", "NodeFailed"])).toBe("succeeded");
  });

  it("re-queue after terminal resets via NodeQueued then NodeStarted", () => {
    expect(laneFor(["NodeFailed", "NodeQueued", "NodeStarted"])).toBe("running");
  });

  it("re-queue after terminal with subsequent completion", () => {
    expect(laneFor(["NodeFailed", "NodeQueued", "NodeStarted", "NodeCompleted"])).toBe("succeeded");
  });

  it("ignores unknown kinds", () => {
    expect(laneFor(["UnknownKind", "NodeCompleted"])).toBe("succeeded");
  });

  it("RunStart and RunEnd are excluded (not op-level kinds)", () => {
    // operationGraph.laneFor only handles op-level Node* kinds
    expect(laneFor(["RunStart", "RunEnd"])).toBe("queued");
  });
});

// ── buildOperationGraph — core fold ──────────────────────────────────────────

describe("buildOperationGraph — empty-op_id exclusion", () => {
  it("ignores events with empty op_id", () => {
    const events = [
      ev("1", "NodeQueued", ""),
      ev("2", "MessageAdded", ""),
      ev("3", "RunStart", ""),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes).toHaveLength(0);
    expect(g.edges).toHaveLength(0);
  });

  it("ignores events whose kind has no op-level mapping", () => {
    const events = [ev("1", "RunStart", "op-a"), ev("2", "RunEnd", "op-a")];
    const g = buildOperationGraph(events);
    expect(g.nodes).toHaveLength(0);
  });
});

describe("buildOperationGraph — status fold", () => {
  it("single queued event", () => {
    const g = buildOperationGraph([ev("1", "NodeQueued", "op-a")]);
    expect(g.nodes).toHaveLength(1);
    expect(g.nodes[0]!.status).toBe("queued");
  });

  it("queued→started→completed yields succeeded", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", {}, 1),
      ev("2", "NodeStarted", "op-a", {}, 2),
      ev("3", "NodeCompleted", "op-a", {}, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.status).toBe("succeeded");
  });

  it("failed then NodeStarted resets to running (re-queue semantics)", () => {
    // terminal allows queued/running — running overrides failed
    const events = [ev("1", "NodeFailed", "op-a", {}, 1), ev("2", "NodeStarted", "op-a", {}, 2)];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.status).toBe("running");
  });

  it("failed then a second terminal is ignored (terminal stickiness)", () => {
    const events = [ev("1", "NodeFailed", "op-a", {}, 1), ev("2", "NodeCompleted", "op-a", {}, 2)];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.status).toBe("failed");
  });
});

describe("buildOperationGraph — name and elapsed extraction", () => {
  it("extracts name from first non-empty payload.name", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", { name: "researcher" }, 1),
      ev("2", "NodeStarted", "op-a", { name: "other-name" }, 2),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.name).toBe("researcher");
  });

  it("name stays empty when payload.name is absent", () => {
    const g = buildOperationGraph([ev("1", "NodeQueued", "op-a", {})]);
    expect(g.nodes[0]!.name).toBe("");
  });

  it("extracts latest (largest) elapsed value", () => {
    const events = [
      ev("1", "NodeStarted", "op-a", { elapsed: 0.5 }, 1),
      ev("2", "NodeCompleted", "op-a", { elapsed: 2.3 }, 2),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.elapsed).toBeCloseTo(2.3);
  });

  it("elapsed defaults to 0 when absent", () => {
    const g = buildOperationGraph([ev("1", "NodeQueued", "op-a", {})]);
    expect(g.nodes[0]!.elapsed).toBe(0);
  });
});

describe("buildOperationGraph — firstTs / lastTs / eventCount", () => {
  it("firstTs is the earliest ts, lastTs is the latest", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", {}, 100),
      ev("2", "NodeStarted", "op-a", {}, 200),
      ev("3", "NodeCompleted", "op-a", {}, 300),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.firstTs).toBe(100);
    expect(g.nodes[0]!.lastTs).toBe(300);
  });

  it("eventCount matches number of op-level events", () => {
    const events = [ev("1", "NodeQueued", "op-a", {}, 1), ev("2", "NodeStarted", "op-a", {}, 2)];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.eventCount).toBe(2);
  });
});

describe("buildOperationGraph — first-seen ordering", () => {
  it("nodes are in first-seen order", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", {}, 1),
      ev("2", "NodeQueued", "op-b", {}, 2),
      ev("3", "NodeQueued", "op-c", {}, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes.map((n) => n.opId)).toEqual(["op-a", "op-b", "op-c"]);
  });

  it("later events for existing op do not change order", () => {
    const events = [
      ev("1", "NodeQueued", "op-b", {}, 1),
      ev("2", "NodeQueued", "op-a", {}, 2),
      ev("3", "NodeStarted", "op-b", {}, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.opId).toBe("op-b");
    expect(g.nodes[1]!.opId).toBe("op-a");
  });
});

describe("buildOperationGraph — cause edges", () => {
  it("builds an edge when cause_op_id is present", () => {
    const events = [
      ev("1", "NodeQueued", "op-parent", {}, 1),
      ev("2", "NodeQueued", "op-child", { cause_op_id: "op-parent" }, 2),
    ];
    const g = buildOperationGraph(events);
    expect(g.edges).toHaveLength(1);
    expect(g.edges[0]).toEqual({ source: "op-parent", target: "op-child" });
  });

  it("edges are deduplicated across events", () => {
    const events = [
      ev("1", "NodeQueued", "op-parent", {}, 1),
      ev("2", "NodeQueued", "op-child", { cause_op_id: "op-parent" }, 2),
      ev("3", "NodeStarted", "op-child", { cause_op_id: "op-parent" }, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.edges).toHaveLength(1);
  });

  it("no edges when cause_op_id is absent", () => {
    const events = [ev("1", "NodeQueued", "op-a", {}, 1), ev("2", "NodeQueued", "op-b", {}, 2)];
    const g = buildOperationGraph(events);
    expect(g.edges).toHaveLength(0);
  });

  it("causeOpId field on node matches payload.cause_op_id", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", {}, 1),
      ev("2", "NodeQueued", "op-b", { cause_op_id: "op-a" }, 2),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[1]!.causeOpId).toBe("op-a");
  });

  it("causeOpId is null when no cause in payload", () => {
    const g = buildOperationGraph([ev("1", "NodeQueued", "op-a", {})]);
    expect(g.nodes[0]!.causeOpId).toBeNull();
  });

  // The engine emits `depends_on` (all predecessors) + `parent_id` (sole
  // predecessor) on Node* signals; `cause_op_id` is never set by that path.
  it("builds an edge from parent_id", () => {
    const events = [
      ev("1", "NodeQueued", "op-parent", {}, 1),
      ev("2", "NodeQueued", "op-child", { parent_id: "op-parent" }, 2),
    ];
    const g = buildOperationGraph(events);
    expect(g.edges).toEqual([{ source: "op-parent", target: "op-child" }]);
    expect(g.nodes[1]!.causeOpId).toBe("op-parent");
  });

  it("builds one edge per predecessor from depends_on (fan-in)", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", {}, 1),
      ev("2", "NodeQueued", "op-b", {}, 2),
      ev("3", "NodeQueued", "op-join", { depends_on: ["op-a", "op-b"] }, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.edges).toHaveLength(2);
    expect(g.edges).toContainEqual({ source: "op-a", target: "op-join" });
    expect(g.edges).toContainEqual({ source: "op-b", target: "op-join" });
  });

  it("dedupes edges across depends_on, parent_id and cause_op_id", () => {
    const events = [
      ev("1", "NodeQueued", "op-parent", {}, 1),
      ev("2", "NodeStarted", "op-child", { depends_on: ["op-parent"], parent_id: "op-parent" }, 2),
      ev("3", "NodeCompleted", "op-child", { cause_op_id: "op-parent" }, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.edges).toEqual([{ source: "op-parent", target: "op-child" }]);
  });

  it("ignores a self-referential predecessor", () => {
    const events = [ev("1", "NodeQueued", "op-a", { depends_on: ["op-a"], parent_id: "op-a" }, 1)];
    const g = buildOperationGraph(events);
    expect(g.edges).toHaveLength(0);
  });

  it("ignores non-string entries in depends_on", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", {}, 1),
      ev("2", "NodeQueued", "op-b", { depends_on: ["op-a", 42, null, ""] }, 2),
    ];
    const g = buildOperationGraph(events);
    expect(g.edges).toEqual([{ source: "op-a", target: "op-b" }]);
  });
});

describe("buildOperationGraph — multiple operations", () => {
  it("handles multiple independent operations", () => {
    const events = [
      ev("1", "NodeQueued", "op-a", { name: "alpha" }, 1),
      ev("2", "NodeQueued", "op-b", { name: "beta" }, 2),
      ev("3", "NodeCompleted", "op-a", { elapsed: 1.5 }, 3),
      ev("4", "NodeFailed", "op-b", {}, 4),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes).toHaveLength(2);
    const alpha = g.nodes.find((n) => n.opId === "op-a")!;
    const beta = g.nodes.find((n) => n.opId === "op-b")!;
    expect(alpha.status).toBe("succeeded");
    expect(alpha.name).toBe("alpha");
    expect(alpha.elapsed).toBeCloseTo(1.5);
    expect(beta.status).toBe("failed");
    expect(beta.name).toBe("beta");
  });
});
