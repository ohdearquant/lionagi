/**
 * Pure geometry tests for the Pulse sparkline layout contract:
 * normalization to the busiest bucket, fixed stacking order,
 * baseline stubs for empty buckets, minimum visible bar height.
 */

import { describe, it, expect } from "vitest";
import {
  BAR_GAP,
  BAR_W,
  CHART_H,
  MIN_BAR_H,
  bucketTotal,
  chartWidth,
  sparklineRects,
} from "./sparkline";
import type { ActivityBucket } from "@/lib/api";

function bucket(partial: Partial<ActivityBucket> & { t: number }): ActivityBucket {
  return { completed: 0, failed: 0, cancelled: 0, running: 0, ...partial };
}

describe("sparklineRects", () => {
  it("empty bucket renders a 1px baseline stub at the axis", () => {
    const rects = sparklineRects([bucket({ t: 0 })]);
    expect(rects).toEqual([{ kind: "stub", x: 0, y: CHART_H - 1, width: BAR_W, height: 1 }]);
  });

  it("busiest bucket spans full chart height; others scale proportionally", () => {
    const rects = sparklineRects([
      bucket({ t: 0, completed: 10 }),
      bucket({ t: 3600, completed: 5 }),
    ]);
    expect(rects[0].height).toBe(CHART_H);
    expect(rects[1].height).toBe(CHART_H / 2);
    expect(rects[1].x).toBe(BAR_W + BAR_GAP);
  });

  it("stacks segments bottom-up in fixed order: completed, failed, cancelled, running", () => {
    const rects = sparklineRects([
      bucket({ t: 0, completed: 1, failed: 1, cancelled: 1, running: 1 }),
    ]);
    expect(rects.map((r) => r.kind)).toEqual(["completed", "failed", "cancelled", "running"]);
    // Bottom-up: completed sits lowest (largest y), running topmost.
    expect(rects[0].y).toBeGreaterThan(rects[3].y);
    // Segments tile the bar exactly: heights sum to the bar height,
    // and the top of the stack is CHART_H minus that height.
    const total = rects.reduce((acc, r) => acc + r.height, 0);
    expect(total).toBeCloseTo(CHART_H);
    expect(rects[3].y).toBeCloseTo(0);
  });

  it("zero-count segments emit no rect", () => {
    const rects = sparklineRects([bucket({ t: 0, completed: 3, running: 2 })]);
    expect(rects.map((r) => r.kind)).toEqual(["completed", "running"]);
  });

  it("tiny buckets next to a busy one keep a visible minimum height", () => {
    const rects = sparklineRects([
      bucket({ t: 0, completed: 1000 }),
      bucket({ t: 3600, failed: 1 }),
    ]);
    expect(rects[1].kind).toBe("failed");
    expect(rects[1].height).toBeGreaterThanOrEqual(MIN_BAR_H);
  });
});

describe("chartWidth / bucketTotal", () => {
  it("width covers n bars with gaps between (no trailing gap)", () => {
    expect(chartWidth(24)).toBe(24 * (BAR_W + BAR_GAP) - BAR_GAP);
  });

  it("never collapses below one bar width", () => {
    expect(chartWidth(0)).toBe(BAR_W);
  });

  it("bucketTotal counts all four segments", () => {
    expect(bucketTotal(bucket({ t: 0, completed: 1, failed: 2, cancelled: 3, running: 4 }))).toBe(
      10,
    );
  });
});
