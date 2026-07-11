import { describe, it, expect } from "vitest";
import {
  buildNodeStatusesByName,
  buildOperationGraph,
  laneFor,
  transitiveReduce,
} from "./operationGraph";
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

  it("NodeStarted → NodePaused → paused, not stuck at running", () => {
    expect(laneFor(["NodeStarted", "NodePaused"])).toBe("paused");
  });

  it("a paused node resumes to running on a subsequent NodeStarted", () => {
    expect(laneFor(["NodeStarted", "NodePaused", "NodeStarted"])).toBe("running");
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

  it("NodeStarted → NodePaused (signal-derived path) reports paused, not running", () => {
    const events = [ev("1", "NodeStarted", "op-a", {}, 1), ev("2", "NodePaused", "op-a", {}, 2)];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.status).toBe("paused");
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

// ── transitiveReduce ─────────────────────────────────────────────────────────

describe("transitiveReduce", () => {
  it("drops the direct edge of a diamond when a longer path covers it", () => {
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "a", target: "c" },
    ];
    expect(transitiveReduce(edges)).toEqual([
      { source: "a", target: "b" },
      { source: "b", target: "c" },
    ]);
  });

  it("reduces a full predecessor chain (A depends on B and C; B depends on C)", () => {
    // Mirrors the engine's depends_on shape: a node can list both its direct
    // predecessor and that predecessor's own predecessor (the full ancestor set).
    const edges = [
      { source: "c", target: "b" },
      { source: "c", target: "a" },
      { source: "b", target: "a" },
    ];
    const reduced = transitiveReduce(edges);
    expect(reduced).toHaveLength(2);
    expect(reduced).not.toContainEqual({ source: "c", target: "a" });
  });

  it("keeps a linear chain intact (nothing redundant)", () => {
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
    ];
    expect(transitiveReduce(edges)).toEqual(edges);
  });

  it("keeps a fan-in with no transitive overlap intact", () => {
    const edges = [
      { source: "w1", target: "j" },
      { source: "w2", target: "j" },
    ];
    expect(transitiveReduce(edges)).toEqual(edges);
  });

  it("does not hang on a cycle", () => {
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "a" },
    ];
    expect(() => transitiveReduce(edges)).not.toThrow();
  });

  it("preserves extra fields on the retained edges", () => {
    const edges = [
      { source: "a", target: "b", id: "e1" },
      { source: "b", target: "c", id: "e2" },
      { source: "a", target: "c", id: "e3" },
    ];
    const reduced = transitiveReduce(edges);
    expect(reduced.map((e) => e.id)).toEqual(["e1", "e2"]);
  });

  it("is a no-op on an empty edge list", () => {
    expect(transitiveReduce([])).toEqual([]);
  });
});

describe("buildOperationGraph — transitive reduction of depends_on edges", () => {
  it("collapses the redundant edge in a diamond", () => {
    const events = [
      ev("1", "NodeCompleted", "a", { name: "a", depends_on: [] }, 1),
      ev("2", "NodeCompleted", "b", { name: "b", depends_on: ["a"] }, 2),
      ev("3", "NodeCompleted", "c", { name: "c", depends_on: ["a", "b"] }, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.edges).toHaveLength(2);
    expect(g.edges).not.toContainEqual({ source: "a", target: "c" });
    expect(g.edges).toContainEqual({ source: "a", target: "b" });
    expect(g.edges).toContainEqual({ source: "b", target: "c" });
  });
});

// ── buildNodeStatusesByName ──────────────────────────────────────────────────

describe("buildNodeStatusesByName", () => {
  it("correlates by payload.name, not op_id", () => {
    const events = [
      ev("1", "NodeStarted", "runtime-uuid-1", { name: "step_a" }, 1),
      ev("2", "NodeCompleted", "runtime-uuid-1", { name: "step_a", elapsed: 2.5 }, 2),
    ];
    const statuses = buildNodeStatusesByName(events);
    expect(statuses.has("runtime-uuid-1")).toBe(false);
    expect(statuses.get("step_a")?.status).toBe("succeeded");
    expect(statuses.get("step_a")?.elapsed).toBeCloseTo(2.5);
  });

  it("ignores events with no authored name", () => {
    const events = [ev("1", "NodeStarted", "runtime-uuid-1", {}, 1)];
    expect(buildNodeStatusesByName(events).size).toBe(0);
  });

  it("reports queued (not running) for a node with only a NodeQueued signal", () => {
    const events = [ev("1", "NodeQueued", "op-1", { name: "step_b" }, 1)];
    expect(buildNodeStatusesByName(events).get("step_b")?.status).toBe("queued");
  });

  it("keeps distinct authored names, from different op_ids, separate", () => {
    const events = [
      ev("1", "NodeStarted", "op-1", { name: "step_a" }, 1),
      ev("2", "NodeFailed", "op-2", { name: "step_b" }, 2),
    ];
    const statuses = buildNodeStatusesByName(events);
    expect(statuses.get("step_a")?.status).toBe("running");
    expect(statuses.get("step_b")?.status).toBe("failed");
  });

  it("NodeStarted → NodePaused (planned/authored-correlation path) reports paused, not running", () => {
    const events = [
      ev("1", "NodeStarted", "runtime-uuid-1", { name: "step_a" }, 1),
      ev("2", "NodePaused", "runtime-uuid-1", { name: "step_a" }, 2),
    ];
    const statuses = buildNodeStatusesByName(events);
    expect(statuses.get("step_a")?.status).toBe("paused");
  });
});
