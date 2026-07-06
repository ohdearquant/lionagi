/**
 * Pure geometry for the Pulse stacked-bar sparkline.
 *
 * Kept free of React so the layout contract (normalization, stacking
 * order, empty-bucket stubs) is testable without rendering.
 */

import type { ActivityBucket } from "@/lib/api";

export const BAR_W = 7;
export const BAR_GAP = 3;
export const CHART_H = 40;
export const MIN_BAR_H = 2;

export const SEGMENT_ORDER = ["completed", "failed", "cancelled", "running"] as const;
export type SegmentKind = (typeof SEGMENT_ORDER)[number];

export interface SparkRect {
  kind: SegmentKind | "stub";
  x: number;
  y: number;
  width: number;
  height: number;
}

export function bucketTotal(b: ActivityBucket): number {
  return b.completed + b.failed + b.cancelled + b.running;
}

/**
 * Completion rate recomputed client-side from the dense per-bucket counts,
 * excluding cancelled runs from the denominator (deliberate stops are not
 * failures) and excluding running entirely (no verdict yet).
 *
 * This intentionally ignores the server's own `completion_rate` field:
 * that field's denominator still includes cancelled runs. It also cannot
 * exclude orphaned (phantom-reaped, daemon-restart) failures from the
 * `failed` count — the backend buckets don't carry a per-row reason
 * breakdown, only aggregate status counts. TODO(unify): once the backend
 * exposes a reason-level split on /api/stats/activity, subtract orphaned
 * failures from `failed` here too.
 */
export function completionRateFromBuckets(buckets: ActivityBucket[]): number | null {
  let completed = 0;
  let failed = 0;
  for (const b of buckets) {
    completed += b.completed;
    failed += b.failed;
  }
  const denom = completed + failed;
  return denom > 0 ? completed / denom : null;
}

export function chartWidth(bucketCount: number): number {
  return Math.max(BAR_W, bucketCount * (BAR_W + BAR_GAP) - BAR_GAP);
}

export function sparklineRects(buckets: ActivityBucket[]): SparkRect[] {
  const max = Math.max(1, ...buckets.map(bucketTotal));
  const rects: SparkRect[] = [];

  buckets.forEach((b, i) => {
    const x = i * (BAR_W + BAR_GAP);
    const total = bucketTotal(b);
    if (total === 0) {
      // Baseline stub so empty buckets still read as part of the axis.
      rects.push({ kind: "stub", x, y: CHART_H - 1, width: BAR_W, height: 1 });
      return;
    }
    const barH = Math.max(MIN_BAR_H, (total / max) * CHART_H);
    let yCursor = CHART_H;
    for (const seg of SEGMENT_ORDER) {
      const n = b[seg];
      if (n === 0) continue;
      const h = (n / total) * barH;
      yCursor -= h;
      rects.push({ kind: seg, x, y: yCursor, width: BAR_W, height: h });
    }
  });

  return rects;
}
