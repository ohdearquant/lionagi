import { describe, it, expect } from "vitest";
import { toposort, hasCycle, validateSpec, emptySpec } from "./validation";
import { workflowDraftReducer } from "@/components/workflow/WorkflowDraftContext";
import type { WorkflowDraftState } from "@/components/workflow/WorkflowDraftContext";
import type { WorkflowSpec, WorkflowEdge } from "@/lib/api";

// ── Helpers ───────────────────────────────────────────────────────────────────

function edge(id: string, from: string, to: string, label?: string): WorkflowEdge {
  return { id, from, to, label };
}

function makeSpec(overrides: Partial<WorkflowSpec> = {}): WorkflowSpec {
  return {
    version: 1,
    nodes: [
      { id: "n1", kind: "input", label: "Input", pos: { x: 64, y: 64 } },
      { id: "n2", kind: "chat", label: "Chat", pos: { x: 320, y: 64 } },
    ],
    edges: [edge("e1", "n1", "n2")],
    inputs: [],
    outputs: [],
    ...overrides,
  };
}

function initialState(spec: WorkflowSpec): WorkflowDraftState {
  return { spec, dirty: false };
}

// ── toposort ─────────────────────────────────────────────────────────────────

describe("toposort", () => {
  it("sorts a simple chain", () => {
    const sorted = toposort(["a", "b", "c"], [edge("e1", "a", "b"), edge("e2", "b", "c")]);
    expect(sorted).toEqual(["a", "b", "c"]);
  });

  it("returns null for a cycle", () => {
    const sorted = toposort(["a", "b"], [edge("e1", "a", "b"), edge("e2", "b", "a")]);
    expect(sorted).toBeNull();
  });

  it("handles isolated nodes", () => {
    const sorted = toposort(["a", "b", "c"], [edge("e1", "a", "c")]);
    expect(sorted).not.toBeNull();
    expect(sorted!).toHaveLength(3);
  });

  it("returns all nodes in a valid linear graph", () => {
    const nodes = ["n1", "n2", "n3", "n4"];
    const edges = [edge("e1", "n1", "n2"), edge("e2", "n2", "n3"), edge("e3", "n3", "n4")];
    const sorted = toposort(nodes, edges);
    expect(sorted).not.toBeNull();
    expect(sorted!.length).toBe(4);
    expect(sorted!.indexOf("n1")).toBeLessThan(sorted!.indexOf("n2"));
    expect(sorted!.indexOf("n2")).toBeLessThan(sorted!.indexOf("n3"));
  });

  it("returns null for a self-loop", () => {
    const sorted = toposort(["a"], [edge("e1", "a", "a")]);
    expect(sorted).toBeNull();
  });

  it("handles empty graph", () => {
    expect(toposort([], [])).toEqual([]);
  });

  it("handles graph with no edges", () => {
    const sorted = toposort(["a", "b", "c"], []);
    expect(sorted).not.toBeNull();
    expect(sorted!).toHaveLength(3);
  });
});

// ── hasCycle ─────────────────────────────────────────────────────────────────

describe("hasCycle", () => {
  it("returns false for acyclic graph", () => {
    expect(hasCycle(["a", "b"], [edge("e1", "a", "b")])).toBe(false);
  });

  it("returns true for a direct cycle", () => {
    expect(hasCycle(["a", "b"], [edge("e1", "a", "b"), edge("e2", "b", "a")])).toBe(true);
  });

  it("returns true for a triangle cycle", () => {
    const edges = [edge("e1", "a", "b"), edge("e2", "b", "c"), edge("e3", "c", "a")];
    expect(hasCycle(["a", "b", "c"], edges)).toBe(true);
  });

  it("returns false for empty graph", () => {
    expect(hasCycle([], [])).toBe(false);
  });
});

// ── validateSpec — rule 1: at least one input node ───────────────────────────

describe("validateSpec — rule: no-input", () => {
  it("passes when there is an input node", () => {
    const errors = validateSpec(makeSpec());
    expect(errors.find((e) => e.rule === "no-input")).toBeUndefined();
  });

  it("fails when no input nodes", () => {
    const spec = makeSpec({
      nodes: [{ id: "n1", kind: "chat", label: "Chat", pos: { x: 0, y: 0 } }],
      edges: [],
    });
    const errors = validateSpec(spec);
    expect(errors.find((e) => e.rule === "no-input")).toBeDefined();
  });
});

// ── validateSpec — rule 2: cycle detection ───────────────────────────────────

describe("validateSpec — rule: cycle", () => {
  it("passes for acyclic spec", () => {
    const errors = validateSpec(makeSpec());
    expect(errors.find((e) => e.rule === "cycle")).toBeUndefined();
  });

  it("fails when spec has a cycle", () => {
    const spec = makeSpec({
      edges: [edge("e1", "n1", "n2"), edge("e2", "n2", "n1")],
    });
    const errors = validateSpec(spec);
    expect(errors.find((e) => e.rule === "cycle")).toBeDefined();
  });
});

// ── validateSpec — rule 3: disconnected nodes ────────────────────────────────

describe("validateSpec — rule: disconnected", () => {
  it("passes for fully connected spec", () => {
    const errors = validateSpec(makeSpec());
    expect(errors.find((e) => e.rule === "disconnected")).toBeUndefined();
  });

  it("fails for disconnected node", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        { id: "n2", kind: "chat", label: "Chat", pos: { x: 200, y: 0 } },
        { id: "n3", kind: "parse", label: "Parse", pos: { x: 400, y: 0 } },
      ],
      edges: [edge("e1", "n1", "n2")],
    });
    const errors = validateSpec(spec);
    expect(errors.find((e) => e.rule === "disconnected")).toBeDefined();
  });

  it("passes for single-node spec (no edges needed)", () => {
    const spec = makeSpec({
      nodes: [{ id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } }],
      edges: [],
    });
    const errors = validateSpec(spec);
    expect(errors.find((e) => e.rule === "disconnected")).toBeUndefined();
  });
});

// ── validateSpec — rule 4: engine_def_id known ───────────────────────────────

describe("validateSpec — rule: engine-no-config / engine-unknown-def", () => {
  it("passes for engine node with known def id", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        {
          id: "n2",
          kind: "engine",
          label: "Engine",
          pos: { x: 200, y: 0 },
          config: { engine_def_id: "def-1" },
        },
      ],
      edges: [edge("e1", "n1", "n2")],
    });
    const errors = validateSpec(spec, new Set(["def-1"]));
    expect(errors.find((e) => e.rule === "engine-unknown-def")).toBeUndefined();
    expect(errors.find((e) => e.rule === "engine-no-config")).toBeUndefined();
  });

  it("fails for engine node with unknown def id", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        {
          id: "n2",
          kind: "engine",
          label: "Engine",
          pos: { x: 200, y: 0 },
          config: { engine_def_id: "unknown" },
        },
      ],
      edges: [edge("e1", "n1", "n2")],
    });
    const errors = validateSpec(spec, new Set(["def-1"]));
    expect(errors.find((e) => e.rule === "engine-unknown-def")).toBeDefined();
  });

  it("fails for engine node without config", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        { id: "n2", kind: "engine", label: "Engine", pos: { x: 200, y: 0 } },
      ],
      edges: [edge("e1", "n1", "n2")],
    });
    const errors = validateSpec(spec, new Set(["def-1"]));
    expect(errors.find((e) => e.rule === "engine-no-config")).toBeDefined();
  });

  it("skips engine check when knownEngineDefIds not provided", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        {
          id: "n2",
          kind: "engine",
          label: "Engine",
          pos: { x: 200, y: 0 },
          config: { engine_def_id: "anything" },
        },
      ],
      edges: [edge("e1", "n1", "n2")],
    });
    const errors = validateSpec(spec);
    expect(errors.find((e) => e.rule === "engine-unknown-def")).toBeUndefined();
  });
});

// ── validateSpec — rule 5: gate fan-out ──────────────────────────────────────

describe("validateSpec — rule: gate-fan-out", () => {
  it("passes for gate with 2 outgoing edges", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        { id: "n2", kind: "gate", label: "Gate", pos: { x: 200, y: 0 } },
        { id: "n3", kind: "chat", label: "Chat A", pos: { x: 400, y: 0 } },
        { id: "n4", kind: "chat", label: "Chat B", pos: { x: 400, y: 100 } },
      ],
      edges: [edge("e1", "n1", "n2"), edge("e2", "n2", "n3", "if"), edge("e3", "n2", "n4", "else")],
    });
    const errors = validateSpec(spec);
    expect(errors.find((e) => e.rule === "gate-fan-out")).toBeUndefined();
  });

  it("fails for gate with 3 outgoing edges", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        { id: "n2", kind: "gate", label: "Gate", pos: { x: 200, y: 0 } },
        { id: "n3", kind: "chat", label: "A", pos: { x: 400, y: 0 } },
        { id: "n4", kind: "chat", label: "B", pos: { x: 400, y: 100 } },
        { id: "n5", kind: "chat", label: "C", pos: { x: 400, y: 200 } },
      ],
      edges: [
        edge("e1", "n1", "n2"),
        edge("e2", "n2", "n3"),
        edge("e3", "n2", "n4"),
        edge("e4", "n2", "n5"),
      ],
    });
    const errors = validateSpec(spec);
    expect(errors.find((e) => e.rule === "gate-fan-out")).toBeDefined();
  });
});

// ── emptySpec ─────────────────────────────────────────────────────────────────

describe("emptySpec", () => {
  it("has exactly one input node", () => {
    const spec = emptySpec();
    expect(spec.nodes.filter((n) => n.kind === "input")).toHaveLength(1);
  });

  it("has version 1", () => {
    expect(emptySpec().version).toBe(1);
  });
});

// ── workflowDraftReducer ──────────────────────────────────────────────────────

describe("workflowDraftReducer — reset", () => {
  it("resets to new spec and clears dirty", () => {
    const spec = emptySpec();
    const dirty = initialState({
      ...spec,
      nodes: [...spec.nodes, { id: "n99", kind: "chat" as const, label: "x", pos: { x: 0, y: 0 } }],
    });
    const next = workflowDraftReducer({ ...dirty, dirty: true }, { type: "reset", spec });
    expect(next.dirty).toBe(false);
    expect(next.spec).toBe(spec);
  });
});

describe("workflowDraftReducer — addNode", () => {
  it("appends a node and sets dirty", () => {
    const spec = emptySpec();
    const state = initialState(spec);
    const newNode = { id: "n2", kind: "chat" as const, label: "Chat", pos: { x: 200, y: 64 } };
    const next = workflowDraftReducer(state, { type: "addNode", node: newNode });
    expect(next.spec.nodes).toHaveLength(2);
    expect(next.spec.nodes[1].id).toBe("n2");
    expect(next.dirty).toBe(true);
  });
});

describe("workflowDraftReducer — removeNode", () => {
  it("removes the node and all connected edges", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, { type: "removeNode", nodeId: "n2" });
    expect(next.spec.nodes.map((n) => n.id)).not.toContain("n2");
    expect(next.spec.edges.filter((e) => e.from === "n2" || e.to === "n2")).toHaveLength(0);
    expect(next.dirty).toBe(true);
  });

  it("no-ops for unknown node id", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, { type: "removeNode", nodeId: "nonexistent" });
    expect(next.spec.nodes).toHaveLength(2);
  });
});

describe("workflowDraftReducer — patchNode", () => {
  it("updates node label", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, {
      type: "patchNode",
      nodeId: "n1",
      patch: { label: "Entry" },
    });
    expect(next.spec.nodes.find((n) => n.id === "n1")?.label).toBe("Entry");
    expect(next.dirty).toBe(true);
  });

  it("does not affect other nodes", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, {
      type: "patchNode",
      nodeId: "n1",
      patch: { label: "Entry" },
    });
    expect(next.spec.nodes.find((n) => n.id === "n2")?.label).toBe("Chat");
  });
});

describe("workflowDraftReducer — moveNode", () => {
  it("snaps position to 16px grid", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, { type: "moveNode", nodeId: "n1", x: 70, y: 70 });
    const n = next.spec.nodes.find((n) => n.id === "n1")!;
    expect(n.pos.x % 16).toBe(0);
    expect(n.pos.y % 16).toBe(0);
  });

  it("sets dirty", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, { type: "moveNode", nodeId: "n1", x: 128, y: 128 });
    expect(next.dirty).toBe(true);
  });
});

describe("workflowDraftReducer — addEdge", () => {
  it("appends edge and sets dirty", () => {
    const spec = makeSpec({
      nodes: [
        { id: "n1", kind: "input", label: "Input", pos: { x: 0, y: 0 } },
        { id: "n2", kind: "chat", label: "Chat", pos: { x: 200, y: 0 } },
        { id: "n3", kind: "parse", label: "Parse", pos: { x: 400, y: 0 } },
      ],
      edges: [edge("e1", "n1", "n2")],
    });
    const state = initialState(spec);
    const next = workflowDraftReducer(state, { type: "addEdge", edge: edge("e2", "n2", "n3") });
    expect(next.spec.edges).toHaveLength(2);
    expect(next.dirty).toBe(true);
  });
});

describe("workflowDraftReducer — patchEdge", () => {
  it("sets a condition on the matching edge", () => {
    const spec = makeSpec({
      edges: [edge("e1", "n1", "n2"), edge("e2", "n2", "n1")],
    });
    const state = initialState(spec);
    const next = workflowDraftReducer(state, {
      type: "patchEdge",
      edgeId: "e1",
      patch: { condition: 'verdict == "APPROVE"' },
    });
    expect(next.spec.edges.find((e) => e.id === "e1")?.condition).toBe('verdict == "APPROVE"');
    expect(next.dirty).toBe(true);
  });

  it("clears a condition when patched with a blank string", () => {
    const spec = makeSpec({
      edges: [{ ...edge("e1", "n1", "n2"), condition: "x > 0" }],
    });
    const state = initialState(spec);
    const next = workflowDraftReducer(state, {
      type: "patchEdge",
      edgeId: "e1",
      patch: { condition: "   " },
    });
    expect(next.spec.edges.find((e) => e.id === "e1")?.condition).toBeUndefined();
  });

  it("does not affect other edges", () => {
    const spec = makeSpec({
      edges: [edge("e1", "n1", "n2"), edge("e2", "n2", "n1")],
    });
    const state = initialState(spec);
    const next = workflowDraftReducer(state, {
      type: "patchEdge",
      edgeId: "e1",
      patch: { condition: "x > 0" },
    });
    expect(next.spec.edges.find((e) => e.id === "e2")?.condition).toBeUndefined();
  });
});

describe("workflowDraftReducer — removeEdge", () => {
  it("removes the edge by id", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, { type: "removeEdge", edgeId: "e1" });
    expect(next.spec.edges).toHaveLength(0);
    expect(next.dirty).toBe(true);
  });

  it("no-ops for unknown edge id", () => {
    const spec = makeSpec();
    const state = initialState(spec);
    const next = workflowDraftReducer(state, { type: "removeEdge", edgeId: "nonexistent" });
    expect(next.spec.edges).toHaveLength(1);
  });
});
