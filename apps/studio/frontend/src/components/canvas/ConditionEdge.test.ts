/**
 * ConditionEdge's routing and muting decisions are pure, data-driven
 * functions — the component just wires their output onto an SVG path. That
 * split lets these be pinned without mounting ReactFlow (getSmoothStepPath /
 * getBezierPath / EdgeLabelRenderer all expect a live ReactFlow provider).
 */
import { describe, it, expect } from "vitest";
import { computeEdgeVisualState, continuationVisualState, isLongRangeEdge } from "./ConditionEdge";

describe("isLongRangeEdge — rank-distance routing threshold", () => {
  it("is short-range under the threshold", () => {
    expect(isLongRangeEdge(0)).toBe(false);
    expect(isLongRangeEdge(1)).toBe(false);
  });

  it("is long-range at and beyond the threshold", () => {
    expect(isLongRangeEdge(2)).toBe(true);
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

describe("continuationVisualState — the fold's return sweep recedes", () => {
  const base = { strokeColor: "var(--dag-edge-done)", strokeOpacity: 1, strokeWidth: 2 };

  it("thins and fades a continuation that is neither hovered nor selected", () => {
    const out = continuationVisualState(base, false);
    expect(out.strokeOpacity).toBeLessThan(base.strokeOpacity);
    expect(out.strokeWidth).toBeLessThan(base.strokeWidth);
  });

  it("hands back the base state untouched once hovered or selected", () => {
    // It is a real dependency underneath, so it has to come back to full
    // strength when someone goes looking at it.
    expect(continuationVisualState(base, true)).toBe(base);
  });

  it("keeps the colour the run state chose", () => {
    // Muting is about weight, not hue: a continuation off a failed step still
    // has to read as belonging to that step.
    const failed = { ...base, strokeColor: "var(--dag-pending-border)" };
    expect(continuationVisualState(failed, false).strokeColor).toBe(failed.strokeColor);
  });

  it("never makes an already-muted edge louder", () => {
    // A completed edge arrives at 0.5. Assigning the continuation opacity
    // outright rather than taking the lower of the two would brighten it.
    const alreadyMuted = { ...base, strokeOpacity: 0.2 };
    expect(continuationVisualState(alreadyMuted, false).strokeOpacity).toBeLessThanOrEqual(0.2);
  });

  it("does not mutate the state it was handed", () => {
    const input = { ...base };
    continuationVisualState(input, false);
    expect(input).toEqual(base);
  });
});
