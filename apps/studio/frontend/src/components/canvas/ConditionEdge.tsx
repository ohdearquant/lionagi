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
}

export function isLongRangeEdge(rankDistance: number | undefined): boolean {
  return (rankDistance ?? 0) >= LONG_RANGE_RANK_DISTANCE;
}

export interface EdgeVisualState {
  strokeColor: string;
  strokeOpacity: number;
  strokeWidth: number;
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
  const isLongRange = isLongRangeEdge(data?.rankDistance);

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
  const { strokeColor, strokeOpacity, strokeWidth } = computeEdgeVisualState(
    Boolean(selected),
    completed,
    emphasized,
  );

  return (
    <>
      <path
        id={id}
        d={edgePath}
        fill="none"
        stroke={strokeColor}
        strokeOpacity={strokeOpacity}
        strokeWidth={strokeWidth}
        strokeDasharray={isCode ? "6 4" : undefined}
        style={{ transition: "stroke 0.25s, stroke-width 0.15s, stroke-opacity 0.25s" }}
        markerEnd={`url(#${completed ? "arrow-active" : "arrow"})`}
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
