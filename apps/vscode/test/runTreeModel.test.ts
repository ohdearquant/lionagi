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
