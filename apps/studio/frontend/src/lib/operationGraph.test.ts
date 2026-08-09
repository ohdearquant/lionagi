import { describe, it, expect } from "vitest";
import {
  buildNodeStatusesByName,
  buildOperationGraph,
  laneFor,
  transitiveReduce,
  transitiveReduceDisplay,
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

// A soft ("fyi" urgency) EscalationRequest resolves to route="notify"
// (lionagi/operations/flow.py::_schedule_escalation) and fires NodeEscalated
// purely for observability — the node keeps working toward its own terminal
// state. laneFor must not map every "NodeEscalated" kind straight to the
// terminal "escalated" lane regardless of route — that pins the op into
// "escalated" forever even after a later NodeCompleted.
describe("laneFor — NodeEscalated route handling", () => {
  it("route=notify (soft help signal) does not move the lane off running", () => {
    expect(laneFor(["NodeStarted", { kind: "NodeEscalated", route: "notify" }])).toBe("running");
  });

  it("route=notify does not block a later NodeCompleted from landing", () => {
    expect(
      laneFor(["NodeStarted", { kind: "NodeEscalated", route: "notify" }, "NodeCompleted"]),
    ).toBe("succeeded");
  });

  it("route=higher_tier (blocked urgency) still escalates", () => {
    expect(laneFor(["NodeStarted", { kind: "NodeEscalated", route: "higher_tier" }])).toBe(
      "escalated",
    );
  });

  it("route=give_up (blocked urgency) still escalates", () => {
    expect(laneFor(["NodeStarted", { kind: "NodeEscalated", route: "give_up" }])).toBe("escalated");
  });

  it("a bare NodeEscalated string (no route) still escalates — back-compat", () => {
    expect(laneFor(["NodeStarted", "NodeEscalated"])).toBe("escalated");
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

describe("buildOperationGraph — NodeEscalated route handling", () => {
  it("a route=notify NodeEscalated does not pin the op into the escalated status", () => {
    const events = [
      ev("1", "NodeStarted", "op-a", {}, 1),
      ev("2", "NodeEscalated", "op-a", { route: "notify" }, 2),
      ev("3", "NodeCompleted", "op-a", {}, 3),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.status).toBe("succeeded");
  });

  it("a route=higher_tier NodeEscalated still reports escalated", () => {
    const events = [
      ev("1", "NodeStarted", "op-a", {}, 1),
      ev("2", "NodeEscalated", "op-a", { route: "higher_tier" }, 2),
    ];
    const g = buildOperationGraph(events);
    expect(g.nodes[0]!.status).toBe("escalated");
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

describe("buildOperationGraph — escalation continuations", () => {
  it("names and links a recorded-shape higher-tier retry without changing lifecycle counts", () => {
    const parentId = "11111111-1111-4111-8111-111111111111";
    const childId = "22222222-2222-4222-8222-222222222222";
    const events = [
      ev("1", "NodeStarted", parentId, { name: "worker" }, 1),
      ev("2", "NodeEscalated", parentId, { name: "worker", route: "higher_tier" }, 2),
      ev("3", "NodeSpawned", childId, { parent_id: parentId, independent: true }, 3),
      ev("4", "NodeQueued", childId, { name: childId.slice(0, 8), depends_on: [] }, 4),
      ev("5", "NodeFailed", "op-broken", { name: "broken" }, 5),
    ];

    const g = buildOperationGraph(events);
    const parent = g.nodes.find((node) => node.opId === parentId)!;
    const child = g.nodes.find((node) => node.opId === childId)!;
    const broken = g.nodes.find((node) => node.opId === "op-broken")!;

    expect(parent.status).toBe("escalated");
    expect(broken.status).toBe("failed");
    expect(child).toMatchObject({
      name: "worker escalation retry",
      status: "queued",
      causeOpId: null,
      eventCount: 1,
    });
    expect(g.edges).toContainEqual({
      source: parentId,
      target: childId,
      continuation: true,
    });
  });

  it("recognizes an out-of-order spawn after the full event fold", () => {
    const events = [
      ev("1", "NodeSpawned", "retry-op", { parent_id: "parent-op", independent: true }, 1),
      ev("2", "NodeQueued", "retry-op", { name: "retry-op" }, 2),
      ev("3", "NodeEscalated", "parent-op", { name: "planner", route: "higher_tier" }, 3),
    ];

    const g = buildOperationGraph(events);
    expect(g.nodes.find((node) => node.opId === "retry-op")?.name).toBe("planner escalation retry");
    expect(g.edges).toContainEqual({
      source: "parent-op",
      target: "retry-op",
      continuation: true,
    });
  });

  it("leaves a soft escalation and an ordinary independent spawn exactly as the signals described them", () => {
    const events = [
      ev("1", "NodeEscalated", "soft-parent", { name: "watcher", route: "notify" }, 1),
      ev("2", "NodeSpawned", "soft-child", { parent_id: "soft-parent", independent: true }, 2),
      ev(
        "3",
        "NodeQueued",
        "soft-child",
        { name: "child", parent_id: "soft-parent", depends_on: ["soft-parent"] },
        3,
      ),
      ev("4", "NodeStarted", "plain-parent", { name: "plain" }, 4),
      ev("5", "NodeSpawned", "plain-child", { parent_id: "plain-parent", independent: true }, 5),
      ev(
        "6",
        "NodeQueued",
        "plain-child",
        { name: "plain-child", parent_id: "plain-parent", depends_on: ["plain-parent"] },
        6,
      ),
    ];

    // Two separate claims, and only the first is about spawn metadata. Neither
    // of these is a higher-tier escalation, so no continuation may be invented
    // for them. But their plain dependency edges were stated outright by
    // NodeQueued's depends_on, and reclassifying a parent link belongs to
    // escalation retries alone — an ordinary independent spawn that declares a
    // dependency still has one.
    const g = buildOperationGraph(events);
    expect(g.edges.filter((e) => e.continuation)).toEqual([]);
    expect(g.edges).toContainEqual({ source: "soft-parent", target: "soft-child" });
    expect(g.edges).toContainEqual({ source: "plain-parent", target: "plain-child" });
  });

  it("keeps a continuation even when a plain dependency path reaches the retry child", () => {
    const events = [
      ev("1", "NodeEscalated", "parent", { name: "worker", route: "higher_tier" }, 1),
      ev("2", "NodeQueued", "middle", { depends_on: ["parent"] }, 2),
      ev("3", "NodeSpawned", "retry", { parent_id: "parent", independent: true }, 3),
      ev("4", "NodeQueued", "retry", { name: "retry", depends_on: ["middle"] }, 4),
    ];

    const g = buildOperationGraph(events);
    expect(g.edges).toContainEqual({ source: "parent", target: "middle" });
    expect(g.edges).toContainEqual({ source: "middle", target: "retry" });
    expect(g.edges).toContainEqual({
      source: "parent",
      target: "retry",
      continuation: true,
    });
  });

  it("does not use a continuation path to remove a plain dependency edge", () => {
    const events = [
      ev("1", "NodeEscalated", "parent", { name: "worker", route: "higher_tier" }, 1),
      ev("2", "NodeSpawned", "retry", { parent_id: "parent", independent: true }, 2),
      ev("3", "NodeQueued", "retry", { name: "retry" }, 3),
      ev("4", "NodeQueued", "downstream", { depends_on: ["parent", "retry"] }, 4),
    ];

    const g = buildOperationGraph(events);
    expect(g.edges).toContainEqual({ source: "parent", target: "downstream" });
    expect(g.edges).toContainEqual({ source: "retry", target: "downstream" });
    expect(g.edges).toContainEqual({
      source: "parent",
      target: "retry",
      continuation: true,
    });
  });

  it("keeps a non-independent spawn link as a plain dependency", () => {
    const events = [
      ev("1", "NodeStarted", "parent", { name: "worker" }, 1),
      ev("2", "NodeSpawned", "child", { parent_id: "parent", independent: false }, 2),
      ev("3", "NodeQueued", "child", { name: "child", parent_id: "parent" }, 3),
    ];

    expect(buildOperationGraph(events).edges).toEqual([{ source: "parent", target: "child" }]);
  });

  it("preserves a readable child name already supplied by a lifecycle signal", () => {
    const events = [
      ev("1", "NodeEscalated", "parent", { name: "worker", route: "higher_tier" }, 1),
      ev("2", "NodeSpawned", "retry", { parent_id: "parent", independent: true }, 2),
      ev("3", "NodeQueued", "retry", { name: "specialized worker" }, 3),
    ];

    expect(buildOperationGraph(events).nodes.find((node) => node.opId === "retry")?.name).toBe(
      "specialized worker",
    );
  });

  it("ignores invalid and self-referential spawn parents", () => {
    const events = [
      ev("1", "NodeEscalated", "parent", { name: "worker", route: "higher_tier" }, 1),
      ev("2", "NodeSpawned", "child-a", { parent_id: 42, independent: true }, 2),
      ev("3", "NodeSpawned", "child-b", { parent_id: "child-b", independent: true }, 3),
      ev("4", "NodeQueued", "child-a", { name: "child-a" }, 4),
      ev("5", "NodeQueued", "child-b", { name: "child-b" }, 5),
    ];

    expect(buildOperationGraph(events).edges).toEqual([]);
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

// ── transitiveReduceDisplay — semantic-guarded display-time reduction ──────────

describe("transitiveReduceDisplay — semantic-guarded display-time reduction", () => {
  it("keeps both branches of a diamond (nothing redundant)", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "A", target: "C" },
      { source: "B", target: "D" },
      { source: "C", target: "D" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toHaveLength(4);
    expect(hidden).toHaveLength(0);
  });

  it("drops a skip edge when a longer path covers it", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "C" },
      { source: "A", target: "C" }, // redundant: A→B→C
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toHaveLength(2);
    expect(kept).not.toContainEqual({ source: "A", target: "C" });
    expect(hidden).toHaveLength(1);
    expect(hidden[0]).toMatchObject({ source: "A", target: "C" });
  });

  it("fan-in to sink: only direct predecessors survive when the sink also depends on the root", () => {
    const edges = [
      { source: "root", target: "w1" },
      { source: "root", target: "w2" },
      { source: "w1", target: "sink" },
      { source: "w2", target: "sink" },
      { source: "root", target: "sink" }, // redundant: root→w1→sink and root→w2→sink
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toHaveLength(4);
    expect(kept).not.toContainEqual({ source: "root", target: "sink" });
    expect(hidden).toHaveLength(1);
    expect(hidden[0]).toMatchObject({ source: "root", target: "sink" });
  });

  it("leaves a cyclic graph entirely unchanged", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "A" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toEqual(edges);
    expect(hidden).toHaveLength(0);
  });

  it("skips reduction for the WHOLE graph even when a transitive-looking edge sits alongside the cycle", () => {
    // A→B→C→A is a cycle; A→C also looks like a candidate for the skip-edge
    // reduction in the diamond/chain tests above (A reaches C via B). A
    // reducer that only guards the cycle's own edges — rather than the whole
    // graph — would still drop A→C here, which this test exists to catch: a
    // cyclic depends_on graph is defensive/unexpected input, and every edge
    // in and around it may be load-bearing, so nothing may be dropped.
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "C" },
      { source: "C", target: "A" },
      { source: "A", target: "C" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toEqual(edges);
    expect(kept).toHaveLength(4);
    expect(hidden).toHaveLength(0);
  });

  it("preserves a conditional edge that would be dropped by plain transitiveReduce", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "C" },
      { source: "A", target: "C", condition: "score > 0.8" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toHaveLength(3);
    expect(kept).toContainEqual(edges[2]); // conditional edge survives
    expect(hidden).toHaveLength(0);
  });

  it("keeps a plain edge whose implying path's FIRST hop is rich", () => {
    // A→B is conditional, so A→B→C is not an unconditional implication —
    // the branch it represents may not execute, so plain A→C is not
    // provably redundant and must survive.
    const edges = [
      { source: "A", target: "B", condition: "x > 0" },
      { source: "B", target: "C" },
      { source: "A", target: "C" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toContainEqual({ source: "A", target: "C" });
    expect(hidden).toHaveLength(0);
  });

  it("keeps a plain edge whose implying path's SECOND hop is rich", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "C", handler: "onMatch" },
      { source: "A", target: "C" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toContainEqual({ source: "A", target: "C" });
    expect(hidden).toHaveLength(0);
  });

  it("still reduces when the ENTIRE implying path is plain (control for the two arms above)", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "C" },
      { source: "A", target: "C" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).not.toContainEqual({ source: "A", target: "C" });
    expect(hidden).toHaveLength(1);
  });

  it("keeps a plain edge whose only implying path runs through a node the caller marks not visible", () => {
    const edges = [
      { source: "A", target: "collapsed" },
      { source: "collapsed", target: "C" },
      { source: "A", target: "C" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges, {
      visibleNodes: new Set(["A", "C"]),
    });
    expect(kept).toContainEqual({ source: "A", target: "C" });
    expect(hidden).toHaveLength(0);
  });

  it("still reduces through that same node when the caller marks it visible", () => {
    const edges = [
      { source: "A", target: "visible" },
      { source: "visible", target: "C" },
      { source: "A", target: "C" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges, {
      visibleNodes: new Set(["A", "visible", "C"]),
    });
    expect(kept).not.toContainEqual({ source: "A", target: "C" });
    expect(hidden).toHaveLength(1);
  });

  it("returns unchanged for an already-minimal graph (identity)", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "C" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toEqual(edges);
    expect(hidden).toHaveLength(0);
  });

  it("is a no-op on an empty edge list", () => {
    expect(transitiveReduceDisplay([])).toEqual({ kept: [], hidden: [] });
  });

  it("never drops a map or handler or mode=code edge, only a bare condition", () => {
    const edges = [
      { source: "A", target: "B" },
      { source: "B", target: "C" },
      { source: "A", target: "C", handler: "onMatch" },
      { source: "A", target: "C", map: { key: "value" } },
      { source: "A", target: "C", mode: "code" },
    ];
    const { kept, hidden } = transitiveReduceDisplay(edges);
    expect(kept).toHaveLength(5);
    expect(hidden).toHaveLength(0);
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
