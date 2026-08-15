import { describe, it, expect } from "vitest";
import {
  computeAuthoredLayers,
  computeElapsedSeconds,
  computeStagePosition,
  deriveProgressCounts,
  formatElapsed,
  reconcileNodeStatuses,
} from "./execGraphProgress";
import { transitiveReduce } from "./operationGraph";
import type { NodeExecStatus } from "@/components/canvas/StepNode";

const ALL_TEN_STATES: NodeExecStatus[] = [
  "pending",
  "queued",
  "running",
  "awaiting_approval",
  "paused",
  "completed",
  "failed",
  "skipped",
  "cancelled",
  "escalated",
];

// ── deriveProgressCounts ────────────────────────────────────────────────────

describe("deriveProgressCounts — summary counts from the canonical status source", () => {
  it("buckets a mixed graph carrying every node state", () => {
    const nodeIds = ALL_TEN_STATES.map((_, i) => `n${i}`);
    const statuses: Record<string, NodeExecStatus> = {};
    ALL_TEN_STATES.forEach((s, i) => (statuses[`n${i}`] = s));

    const counts = deriveProgressCounts(nodeIds, statuses);

    expect(counts.total).toBe(10);
    expect(counts.pending).toBe(1);
    expect(counts.queued).toBe(1);
    expect(counts.running).toBe(1);
    expect(counts.awaitingApproval).toBe(1);
    expect(counts.paused).toBe(1);
    expect(counts.completed).toBe(1);
    expect(counts.failed).toBe(1);
    expect(counts.skipped).toBe(1);
    expect(counts.cancelled).toBe(1);
    expect(counts.escalated).toBe(1);
    expect(counts.hasFailure).toBe(true);
  });

  it("sums every bucket back to total on a mixed graph", () => {
    const nodeIds = ALL_TEN_STATES.map((_, i) => `n${i}`);
    const statuses: Record<string, NodeExecStatus> = {};
    ALL_TEN_STATES.forEach((s, i) => (statuses[`n${i}`] = s));
    const counts = deriveProgressCounts(nodeIds, statuses);
    const sum =
      counts.pending +
      counts.queued +
      counts.running +
      counts.awaitingApproval +
      counts.paused +
      counts.completed +
      counts.skipped +
      counts.cancelled +
      counts.escalated +
      counts.failed;
    expect(sum).toBe(counts.total);
  });

  it("yields all-zero counts for empty input", () => {
    const counts = deriveProgressCounts([], {});
    expect(counts).toEqual({
      total: 0,
      pending: 0,
      queued: 0,
      running: 0,
      awaitingApproval: 0,
      paused: 0,
      completed: 0,
      skipped: 0,
      cancelled: 0,
      escalated: 0,
      failed: 0,
      hasFailure: false,
    });
  });

  it("defaults a node with no entry in the status map to pending, same as WorkerCanvas", () => {
    const counts = deriveProgressCounts(["a", "b"], { a: "completed" });
    expect(counts.pending).toBe(1);
    expect(counts.completed).toBe(1);
    expect(counts.total).toBe(2);
  });

  it("treats a fully successful run as hasFailure=false", () => {
    const counts = deriveProgressCounts(["a", "b"], { a: "completed", b: "completed" });
    expect(counts.hasFailure).toBe(false);
  });

  it("counts escalated separately while genuine failed remains a failure", () => {
    const paired = deriveProgressCounts(["a", "b"], { a: "escalated", b: "failed" });
    expect(paired.escalated).toBe(1);
    expect(paired.failed).toBe(1);
    expect(paired.hasFailure).toBe(true);

    const escalatedOnly = deriveProgressCounts(["a"], { a: "escalated" });
    expect(escalatedOnly.escalated).toBe(1);
    expect(escalatedOnly.failed).toBe(0);
    expect(escalatedOnly.hasFailure).toBe(false);
  });
});

// ── computeElapsedSeconds / formatElapsed ───────────────────────────────────

describe("computeElapsedSeconds — elapsed derivation", () => {
  it("returns null when the run has not started", () => {
    expect(computeElapsedSeconds(null, null, 100)).toBeNull();
  });

  it("uses now as the end for a live (unfinished) run", () => {
    expect(computeElapsedSeconds(10, null, 42)).toBe(32);
  });

  it("uses ended_at once the run has finished, ignoring now", () => {
    expect(computeElapsedSeconds(10, 25, 9999)).toBe(15);
  });

  it("never returns negative elapsed for a clock skew edge case", () => {
    expect(computeElapsedSeconds(50, null, 10)).toBe(0);
  });
});

describe("formatElapsed", () => {
  it("renders sub-hour elapsed as mm:ss", () => {
    expect(formatElapsed(65)).toBe("01:05");
  });

  it("renders hour-plus elapsed with an hour segment", () => {
    expect(formatElapsed(3725)).toBe("1:02:05");
  });

  it("renders the null (not-started) case as a placeholder, not a crash", () => {
    expect(formatElapsed(null)).toBe("--:--");
  });

  it("treats NaN and Infinity as invalid display inputs", () => {
    expect(formatElapsed(NaN)).toBe("--:--");
    expect(formatElapsed(Infinity)).toBe("--:--");
    expect(formatElapsed(-Infinity)).toBe("--:--");
  });

  it("treats a negative duration as an invalid display input", () => {
    expect(formatElapsed(-5)).toBe("--:--");
  });
});

// ── reconcileNodeStatuses — descendant-terminal suppression + terminal-run collapse ──

describe("reconcileNodeStatuses — terminal-run unknown status", () => {
  it("a node with no signal at all renders as pending on a terminal run", () => {
    const result = reconcileNodeStatuses(["a", "b"], [], { a: "completed" }, true);
    expect(result.b).toBe("pending");
  });

  it("collapses any lingering non-terminal status to pending once the run is done", () => {
    const statuses: Record<string, NodeExecStatus> = {
      a: "queued",
      b: "awaiting_approval",
      c: "paused",
    };
    const result = reconcileNodeStatuses(["a", "b", "c"], [], statuses, true);
    expect(result.a).toBe("pending");
    expect(result.b).toBe("pending");
    expect(result.c).toBe("pending");
  });

  it("leaves terminal statuses untouched once the run is done", () => {
    const statuses: Record<string, NodeExecStatus> = {
      a: "completed",
      b: "failed",
      c: "escalated",
    };
    const result = reconcileNodeStatuses(["a", "b", "c"], [], statuses, true);
    expect(result).toEqual(statuses);
  });

  it("does not touch a live (not-done) run's legitimate non-terminal statuses", () => {
    const statuses: Record<string, NodeExecStatus> = { a: "queued", b: "awaiting_approval" };
    const result = reconcileNodeStatuses(["a", "b"], [], statuses, false);
    expect(result.a).toBe("queued");
    expect(result.b).toBe("awaiting_approval");
  });
});

describe("reconcileNodeStatuses — evidence-named failed ops outrank stale lanes", () => {
  it("marks the op the run's failure evidence names as failed even when its lane is queued", () => {
    const statuses: Record<string, NodeExecStatus> = { critic: "queued", explorer: "running" };
    const result = reconcileNodeStatuses(
      ["critic", "explorer"],
      [],
      statuses,
      true,
      new Set(["critic"]),
    );
    expect(result.critic).toBe("failed");
    expect(result.explorer).toBe("pending");
  });

  it("applies evidence even on a live run and never marks unnamed nodes", () => {
    const statuses: Record<string, NodeExecStatus> = { a: "running", b: "completed" };
    const result = reconcileNodeStatuses(["a", "b"], [], statuses, false, new Set(["a"]));
    expect(result.a).toBe("failed");
    expect(result.b).toBe("completed");
  });
});

describe("reconcileNodeStatuses — descendant-terminal suppression of stale running display", () => {
  // source -> mid -> sink, mirroring the measured defect: 5 root/mid nodes
  // stuck "running" forever while their descendants had already finished.
  const edges = [
    { source: "source", target: "mid" },
    { source: "mid", target: "sink" },
  ];

  it("a running node whose descendant completed is not shown as running (terminal run)", () => {
    const statuses: Record<string, NodeExecStatus> = {
      source: "running",
      mid: "running",
      sink: "completed",
    };
    const result = reconcileNodeStatuses(["source", "mid", "sink"], edges, statuses, true);
    expect(result.source).not.toBe("running");
    expect(result.mid).not.toBe("running");
    expect(["completed", "pending"]).toContain(result.source);
    expect(["completed", "pending"]).toContain(result.mid);
  });

  it("holds on a still-live run too — the invariant is not done-gated", () => {
    const statuses: Record<string, NodeExecStatus> = {
      source: "running",
      mid: "running",
      sink: "completed",
    };
    const result = reconcileNodeStatuses(["source", "mid", "sink"], edges, statuses, false);
    expect(result.source).not.toBe("running");
    expect(result.mid).not.toBe("running");
  });

  // A cancellation reaches nodes that never ran, ahead of dependency waiting,
  // so a cancelled descendant says nothing about whether its ancestor finished.
  // Reading it as evidence reported interrupted work as completed.
  it("does not suppress a running ancestor whose descendant was cancelled", () => {
    const statuses: Record<string, NodeExecStatus> = {
      a: "running",
      b: "cancelled",
    };
    const result = reconcileNodeStatuses(
      ["a", "b"],
      [{ source: "a", target: "b" }],
      statuses,
      false,
    );
    expect(result.a).toBe("running");
    expect(result.b).toBe("cancelled");
  });

  it("still suppresses when a cancelled descendant sits beside one that really ran", () => {
    // The evidence is per-descendant: one that proves the ancestor finished is
    // enough, and the cancelled sibling neither adds to nor cancels it out.
    const statuses: Record<string, NodeExecStatus> = {
      a: "running",
      b: "cancelled",
      c: "completed",
    };
    const result = reconcileNodeStatuses(
      ["a", "b", "c"],
      [
        { source: "a", target: "b" },
        { source: "a", target: "c" },
      ],
      statuses,
      false,
    );
    expect(result.a).not.toBe("running");
  });

  it("keeps cancelled terminal for the done-collapse — it is not rewritten to pending", () => {
    // Excluding cancelled from the suppression evidence must not make it read
    // as still-active work when the run has finished.
    const statuses: Record<string, NodeExecStatus> = { a: "running", b: "cancelled" };
    const result = reconcileNodeStatuses(
      ["a", "b"],
      [{ source: "a", target: "b" }],
      statuses,
      true,
    );
    expect(result.b).toBe("cancelled");
  });

  it("does not suppress a genuinely running node with no terminal descendant", () => {
    const statuses: Record<string, NodeExecStatus> = {
      source: "completed",
      mid: "running",
      sink: "pending",
    };
    const result = reconcileNodeStatuses(["source", "mid", "sink"], edges, statuses, false);
    expect(result.mid).toBe("running");
  });

  it("looks through multiple hops, not just direct children", () => {
    const chain = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "c", target: "d" },
    ];
    const statuses: Record<string, NodeExecStatus> = {
      a: "running",
      b: "running",
      c: "running",
      d: "completed",
    };
    const result = reconcileNodeStatuses(["a", "b", "c", "d"], chain, statuses, false);
    expect(result.a).not.toBe("running");
    expect(result.b).not.toBe("running");
    expect(result.c).not.toBe("running");
  });

  it("survives a cyclic edge list without hanging (defensive — graph is expected acyclic)", () => {
    const cyclic = [
      { source: "a", target: "b" },
      { source: "b", target: "a" },
    ];
    const statuses: Record<string, NodeExecStatus> = { a: "running", b: "running" };
    expect(() => reconcileNodeStatuses(["a", "b"], cyclic, statuses, false)).not.toThrow();
  });
});

// ── computeAuthoredLayers / computeStagePosition ────────────────────────────

describe("computeAuthoredLayers", () => {
  it("returns one stage for an empty graph", () => {
    expect(computeAuthoredLayers([], [])).toEqual([]);
  });

  it("returns one stage for a single node", () => {
    expect(computeAuthoredLayers(["a"], [])).toEqual([["a"]]);
  });

  it("assigns ranks 1..5 for a linear pipeline of 5 nodes", () => {
    const nodeIds = ["a", "b", "c", "d", "e"];
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "c", target: "d" },
      { source: "d", target: "e" },
    ];
    const layers = computeAuthoredLayers(nodeIds, edges);
    expect(layers.map((l) => l.length)).toEqual([1, 1, 1, 1, 1]);
    expect(layers).toEqual([["a"], ["b"], ["c"], ["d"], ["e"]]);
  });

  it("places diamond branches sharing a rank on the same layer", () => {
    // a -> {b, c} -> d
    const nodeIds = ["a", "b", "c", "d"];
    const edges = [
      { source: "a", target: "b" },
      { source: "a", target: "c" },
      { source: "b", target: "d" },
      { source: "c", target: "d" },
    ];
    const layers = computeAuthoredLayers(nodeIds, edges);
    expect(layers[0]).toEqual(["a"]);
    expect(new Set(layers[1])).toEqual(new Set(["b", "c"]));
    expect(layers[2]).toEqual(["d"]);
  });

  it("keeps ranks-of-record unchanged after transitive reduction of the same edges", () => {
    // a -> b -> c, plus a redundant a -> c edge (implied by the path above).
    const nodeIds = ["a", "b", "c"];
    const fullEdges = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "a", target: "c" },
    ];
    const reduced = transitiveReduce(fullEdges);
    expect(reduced.length).toBeLessThan(fullEdges.length); // sanity: reduction actually dropped one

    const layersFull = computeAuthoredLayers(nodeIds, fullEdges);
    const layersReduced = computeAuthoredLayers(nodeIds, reduced);
    const depthOf = (layers: string[][], id: string) => layers.findIndex((l) => l.includes(id));
    for (const id of nodeIds) {
      expect(depthOf(layersReduced, id)).toBe(depthOf(layersFull, id));
    }
  });
});

describe("computeStagePosition", () => {
  const linear = {
    nodeIds: ["a", "b", "c", "d", "e"],
    edges: [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "c", target: "d" },
      { source: "d", target: "e" },
    ],
  };

  it("reports one stage for an empty graph", () => {
    expect(computeStagePosition([], [], {}, false)).toEqual({ stage: 0, totalStages: 0 });
  });

  it("reports one stage for a single node", () => {
    expect(computeStagePosition(["a"], [], { a: "running" }, false)).toEqual({
      stage: 1,
      totalStages: 1,
    });
  });

  it("is at rank 3 of 5 when the middle node of a linear pipeline is running", () => {
    const statuses: Record<string, NodeExecStatus> = {
      a: "completed",
      b: "completed",
      c: "running",
      d: "pending",
      e: "pending",
    };
    expect(computeStagePosition(linear.nodeIds, linear.edges, statuses, false)).toEqual({
      stage: 3,
      totalStages: 5,
    });
  });

  it("reports the final stage once the run is done", () => {
    const statuses: Record<string, NodeExecStatus> = {
      a: "completed",
      b: "completed",
      c: "completed",
      d: "completed",
      e: "completed",
    };
    expect(computeStagePosition(linear.nodeIds, linear.edges, statuses, true)).toEqual({
      stage: 5,
      totalStages: 5,
    });
  });
});
