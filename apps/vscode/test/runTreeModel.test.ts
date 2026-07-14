/**
 * Unit tests for the run-tree signal reducer (src/runs/runTreeModel.ts),
 * focused on the paused lifecycle lane: NodePaused parks a running node in
 * "paused", NodeStarted resumes it, and terminal states stay sticky against
 * a late NodePaused.
 */
import { describe, it, expect } from "vitest";
import {
  applySignalRow,
  createRunTreeState,
} from "../src/runs/runTreeModel.js";
import type { SignalRow } from "../src/api/types.js";

function row(kind: string, opId: string, extra: Record<string, unknown> = {}): SignalRow {
  return {
    seq: 0,
    kind,
    op_id: opId,
    payload: { op_id: opId, ...extra },
  } as SignalRow;
}

describe("runTreeModel paused lane", () => {
  it("pauses a running node and resumes it on NodeStarted", () => {
    const state = createRunTreeState();
    applySignalRow(state, row("NodeStarted", "op1", { name: "worker" }));
    expect(state.nodes.get("op1")?.state).toBe("running");

    applySignalRow(state, row("NodePaused", "op1"));
    expect(state.nodes.get("op1")?.state).toBe("paused");

    applySignalRow(state, row("NodeStarted", "op1"));
    expect(state.nodes.get("op1")?.state).toBe("running");
  });

  it("keeps terminal states sticky against a late NodePaused", () => {
    const state = createRunTreeState();
    applySignalRow(state, row("NodeStarted", "op1"));
    applySignalRow(state, row("NodeCompleted", "op1"));
    expect(state.nodes.get("op1")?.state).toBe("succeeded");

    applySignalRow(state, row("NodePaused", "op1"));
    expect(state.nodes.get("op1")?.state).toBe("succeeded");
  });

  it("upserts an unseen node on NodePaused", () => {
    const state = createRunTreeState();
    applySignalRow(state, row("NodePaused", "op9", { name: "late" }));
    expect(state.nodes.get("op9")?.state).toBe("paused");
    expect(state.order).toContain("op9");
  });
});

// A soft ("fyi" urgency) EscalationRequest resolves to route="notify"
// (lionagi/operations/flow.py::_schedule_escalation) and fires NodeEscalated
// purely for observability — the node keeps working toward its own terminal
// state. applySignalRow must not treat every NodeEscalated as an
// unconditional, terminal "escalated" transition (no route check) — that
// pins the op into displaying "escalated" forever even after it later
// emits NodeCompleted.
describe("runTreeModel NodeEscalated route handling", () => {
  it("route=notify (soft help signal) does not change the node's lane", () => {
    const state = createRunTreeState();
    applySignalRow(state, row("NodeStarted", "op1", { name: "worker" }));
    expect(state.nodes.get("op1")?.state).toBe("running");

    applySignalRow(state, row("NodeEscalated", "op1", { route: "notify" }));
    expect(state.nodes.get("op1")?.state).toBe("running");

    applySignalRow(state, row("NodeCompleted", "op1"));
    expect(state.nodes.get("op1")?.state).toBe("succeeded");
  });

  it("route=higher_tier (blocked urgency) still marks the node escalated", () => {
    const state = createRunTreeState();
    applySignalRow(state, row("NodeStarted", "op1", { name: "worker" }));
    applySignalRow(state, row("NodeEscalated", "op1", { route: "higher_tier" }));
    expect(state.nodes.get("op1")?.state).toBe("escalated");
  });

  it("a bare NodeEscalated with no route still marks the node escalated", () => {
    const state = createRunTreeState();
    applySignalRow(state, row("NodeStarted", "op1", { name: "worker" }));
    applySignalRow(state, row("NodeEscalated", "op1"));
    expect(state.nodes.get("op1")?.state).toBe("escalated");
  });
});
