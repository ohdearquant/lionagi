"use client";

import { memo } from "react";
import { Handle, Position } from "reactflow";
import type { NodeProps } from "reactflow";

const ROLE_COLORS: Record<string, string> = {
  researcher: "#22c55e",
  implementer: "#a855f7",
  reviewer: "#14b8a6",
  critic: "#f59e0b",
  analyst: "#3b82f6",
  architect: "#ec4899",
  tester: "#06b6d4",
};

export interface StepNodeData {
  label: string;
  role: string;
  assignment: string;
  prompt: string;
  capacity: number;
  timeout: number | null;
  inputs: string[];
  outputs: string[];
  execStatus?: "pending" | "running" | "completed" | "failed";
}

function StepNodeComponent({ data, selected }: NodeProps<StepNodeData>) {
  const roleColor = ROLE_COLORS[data.role] || "#666";
  const status = data.execStatus ?? "pending";

  const borderColor =
    status === "running"
      ? "#60a5fa"
      : status === "completed"
        ? "#22c55e"
        : status === "failed"
          ? "#ef4444"
          : selected
            ? "#a855f7"
            : "#333";

  const bgColor =
    status === "running"
      ? "#1a1a3e"
      : status === "completed"
        ? "#0a2e1a"
        : status === "failed"
          ? "#2e0a0a"
          : "#0d0d0d";

  return (
    <div
      className="relative rounded-lg px-4 py-3"
      style={{
        background: bgColor,
        border: `${selected || status === "running" ? 2 : 1}px solid ${borderColor}`,
        minWidth: 180,
        maxWidth: 240,
        transition: "border-color 0.2s, background 0.2s",
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!w-2.5 !h-2.5 !bg-neutral-600 !border-neutral-500 hover:!bg-neutral-400"
      />

      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-sm font-semibold text-neutral-200 truncate">
          {data.label}
        </span>
        {status === "completed" && (
          <span className="text-green-400 text-xs shrink-0">&#10003;</span>
        )}
        {status === "failed" && <span className="text-red-400 text-xs shrink-0">&#10007;</span>}
      </div>

      {data.role && (
        <span
          className="inline-block mt-1 px-2 py-0.5 rounded-full text-[10px] font-mono"
          style={{
            backgroundColor: `${roleColor}20`,
            color: roleColor,
          }}
        >
          {data.role}
        </span>
      )}

      {data.assignment && (
        <div className="mt-1.5 font-mono text-[11px] text-neutral-500 truncate">
          {data.assignment}
        </div>
      )}

      {status === "running" && (
        <div
          className="absolute inset-0 rounded-lg pointer-events-none"
          style={{
            border: "2px solid #60a5fa",
            animation: "pulse 1.5s ease-in-out infinite",
            opacity: 0.4,
          }}
        />
      )}

      <Handle
        type="source"
        position={Position.Right}
        className="!w-2.5 !h-2.5 !bg-neutral-600 !border-neutral-500 hover:!bg-neutral-400"
      />
    </div>
  );
}

export default memo(StepNodeComponent);
