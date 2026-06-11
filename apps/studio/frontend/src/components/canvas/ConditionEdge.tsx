"use client";

import { memo } from "react";
import { getBezierPath, EdgeLabelRenderer } from "reactflow";
import type { EdgeProps } from "reactflow";

export interface ConditionEdgeData {
  mode: "simple" | "code";
  condition?: string;
  map?: Record<string, string>;
  handler?: string;
  sourceCompleted?: boolean;
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
  const completed = data?.sourceCompleted ?? false;
  const isCode = data?.mode === "code";

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  const strokeColor = selected ? "#a855f7" : completed ? "#22c55e" : "#444";

  return (
    <>
      <path
        id={id}
        d={edgePath}
        fill="none"
        stroke={strokeColor}
        strokeWidth={selected ? 2.5 : completed ? 2 : 1.5}
        strokeDasharray={isCode ? "6 4" : undefined}
        style={{ transition: "stroke 0.3s, stroke-width 0.2s" }}
        markerEnd={`url(#${completed ? "arrow-active" : "arrow"})`}
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
              className="rounded px-1.5 py-0.5 text-[10px] font-mono"
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
