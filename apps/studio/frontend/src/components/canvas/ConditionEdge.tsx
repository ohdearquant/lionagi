"use client";

import { memo, useState } from "react";
import { getBezierPath, getSmoothStepPath, EdgeLabelRenderer } from "reactflow";
import type { EdgeProps } from "reactflow";

// A bezier between ranks that are far apart draws one long curve sweeping
// across every intervening rank's cards — the "spaghetti" a deep chain reads
// as. Past this rank distance, route with a rounded step instead: it hugs the
// rank grid rather than cutting across it.
const LONG_RANGE_RANK_DISTANCE = 3;
const LONG_RANGE_BORDER_RADIUS = 12;
const LONG_RANGE_OFFSET = 24;

export interface ConditionEdgeData {
  mode: "simple" | "code";
  condition?: string;
  map?: Record<string, string>;
  handler?: string;
  sourceCompleted?: boolean;
  /** Longest-path rank distance source -> target, from useLayout's rank map
   * (WorkerCanvas attaches this after each layout pass). Undefined for edges
   * that predate a layout — e.g. a fresh onConnect in the editor — which
   * fall back to the short-edge bezier route. */
  rankDistance?: number;
  /** Set by the layout when folding put this edge's target on the row below
   * its source, so it sweeps back across the canvas. See markContinuationEdges. */
  continuation?: boolean;
}

export function isLongRangeEdge(rankDistance: number | undefined): boolean {
  return (rankDistance ?? 0) >= LONG_RANGE_RANK_DISTANCE;
}

export interface EdgeVisualState {
  strokeColor: string;
  strokeOpacity: number;
  strokeWidth: number;
}

// A continuation carries no information of its own: the reader already knows
// the run continues, the same way they know a sentence continues on the next
// line. So it is drawn to be followed and not read — finely dotted, thin, and
// well under the weight of the dependencies around it. The dot pattern is
// tighter than the dashes a code-mode condition uses, so the two never read as
// the same kind of line. Hover and selection still restore it to full strength,
// because it is a real dependency underneath and has to stay inspectable.
const CONTINUATION_DASHARRAY = "2 6";
const CONTINUATION_OPACITY = 0.35;
const CONTINUATION_WIDTH = 1.25;

export function continuationVisualState(
  base: EdgeVisualState,
  emphasized: boolean,
): EdgeVisualState {
  if (emphasized) return base;
  return {
    ...base,
    strokeOpacity: Math.min(base.strokeOpacity, CONTINUATION_OPACITY),
    strokeWidth: CONTINUATION_WIDTH,
  };
}

// Completed edges recede once a run is done — a finished 18-node graph with
// every edge at full green saturation reads as uniformly "hot" and buries
// the one thing worth looking at. Selection always wins (a reviewer clicked
// it); hover re-emphasizes so a muted edge stays inspectable without
// clicking. Non-completed (running/pending) edges are never muted.
export function computeEdgeVisualState(
  selected: boolean,
  completed: boolean,
  emphasized: boolean,
): EdgeVisualState {
  return {
    strokeColor: selected
      ? "var(--status-selected)"
      : completed
        ? "var(--dag-edge-done)"
        : "var(--dag-pending-border)",
    strokeOpacity: completed && !emphasized ? 0.5 : 1,
    strokeWidth: selected ? 2.5 : completed ? 1.75 : 2,
  };
}

function ConditionEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  selected,
}: EdgeProps<ConditionEdgeData>) {
  const [hovered, setHovered] = useState(false);
  const completed = data?.sourceCompleted ?? false;
  const isCode = data?.mode === "code";
  const isContinuation = data?.continuation === true;
  // A bezier drawn to a target that sits left of its source doubles back
  // through its own start; the stepped route is the only one that reads as a
  // return sweep, so a continuation takes it whatever its rank distance.
  const isLongRange = isContinuation || isLongRangeEdge(data?.rankDistance);

  const [edgePath, labelX, labelY] = isLongRange
    ? getSmoothStepPath({
        sourceX,
        sourceY,
        sourcePosition,
        targetX,
        targetY,
        targetPosition,
        borderRadius: LONG_RANGE_BORDER_RADIUS,
        offset: LONG_RANGE_OFFSET,
      })
    : getBezierPath({
        sourceX,
        sourceY,
        sourcePosition,
        targetX,
        targetY,
        targetPosition,
      });

  const emphasized = Boolean(selected) || hovered;
  const base = computeEdgeVisualState(Boolean(selected), completed, emphasized);
  const { strokeColor, strokeOpacity, strokeWidth } = isContinuation
    ? continuationVisualState(base, emphasized)
    : base;

  return (
    <>
      <path
        id={id}
        d={edgePath}
        fill="none"
        stroke={strokeColor}
        strokeOpacity={strokeOpacity}
        strokeWidth={strokeWidth}
        strokeDasharray={isContinuation ? CONTINUATION_DASHARRAY : isCode ? "6 4" : undefined}
        style={{ transition: "stroke 0.25s, stroke-width 0.15s, stroke-opacity 0.25s" }}
        // An arrowhead is what makes a backwards edge read as a dependency on
        // something already behind you. A wrapped line of text does not point
        // back at itself either.
        markerEnd={isContinuation ? undefined : `url(#${completed ? "arrow-active" : "arrow"})`}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      />

      {data?.condition && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan pointer-events-auto cursor-pointer"
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
          >
            <span
              className="rounded px-1.5 py-0.5 font-mono text-[length:var(--t-xs)]"
              style={{
                backgroundColor: selected ? "#2d1b69" : "#1a1a1a",
                color: selected ? "#c4b5fd" : "#888",
                border: `1px solid ${selected ? "#7c3aed" : "#333"}`,
              }}
            >
              {data.condition}
            </span>
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export default memo(ConditionEdgeComponent);
