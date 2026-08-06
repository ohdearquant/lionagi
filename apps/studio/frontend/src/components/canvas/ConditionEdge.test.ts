/**
 * ConditionEdge's routing and muting decisions are pure, data-driven
 * functions — the component just wires their output onto an SVG path. That
 * split lets these be pinned without mounting ReactFlow (getSmoothStepPath /
 * getBezierPath / EdgeLabelRenderer all expect a live ReactFlow provider).
 */
import { describe, it, expect } from "vitest";
import { computeEdgeVisualState, isLongRangeEdge } from "./ConditionEdge";

describe("isLongRangeEdge — rank-distance routing threshold", () => {
  it("is short-range under the threshold", () => {
    expect(isLongRangeEdge(0)).toBe(false);
    expect(isLongRangeEdge(1)).toBe(false);
    expect(isLongRangeEdge(2)).toBe(false);
  });

  it("is long-range at and beyond the threshold", () => {
    expect(isLongRangeEdge(3)).toBe(true);
    expect(isLongRangeEdge(4)).toBe(true);
    expect(isLongRangeEdge(10)).toBe(true);
  });

  it("treats undefined rank distance (edge predates a layout pass) as short-range", () => {
    expect(isLongRangeEdge(undefined)).toBe(false);
  });

  it("treats a negative rank distance (shouldn't happen, but defensively) as short-range", () => {
    expect(isLongRangeEdge(-1)).toBe(false);
  });
});

describe("computeEdgeVisualState — completed edges mute, active/selected stay emphasized", () => {
  it("a pending/running edge is at full emphasis", () => {
    const state = computeEdgeVisualState(false, false, false);
    expect(state.strokeOpacity).toBe(1);
    expect(state.strokeColor).toBe("var(--dag-pending-border)");
  });

  it("a completed edge is muted when not emphasized", () => {
    const state = computeEdgeVisualState(false, true, false);
    expect(state.strokeOpacity).toBeLessThan(1);
    expect(state.strokeColor).toBe("var(--dag-edge-done)");
  });

  it("hover re-emphasizes a completed edge back to full opacity", () => {
    const state = computeEdgeVisualState(false, true, true);
    expect(state.strokeOpacity).toBe(1);
  });

  it("selection always wins — full opacity and the selected color, completed or not", () => {
    const selectedPending = computeEdgeVisualState(true, false, true);
    const selectedCompleted = computeEdgeVisualState(true, true, true);
    expect(selectedPending.strokeOpacity).toBe(1);
    expect(selectedCompleted.strokeOpacity).toBe(1);
    expect(selectedPending.strokeColor).toBe("var(--status-selected)");
    expect(selectedCompleted.strokeColor).toBe("var(--status-selected)");
  });

  it("a completed edge is thinner than an active one, and selected is thickest", () => {
    const pending = computeEdgeVisualState(false, false, false);
    const completed = computeEdgeVisualState(false, true, false);
    const selected = computeEdgeVisualState(true, false, false);
    expect(completed.strokeWidth).toBeLessThan(pending.strokeWidth);
    expect(selected.strokeWidth).toBeGreaterThan(pending.strokeWidth);
  });
});
