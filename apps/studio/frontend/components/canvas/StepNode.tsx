"use client";

import { memo } from "react";
import { Handle, Position } from "reactflow";
import type { NodeProps } from "reactflow";

const ROLE_VAR: Record<string, string> = {
  researcher: "var(--role-researcher)",
  implementer: "var(--role-implementer)",
  reviewer: "var(--role-reviewer)",
  critic: "var(--role-critic)",
  analyst: "var(--role-analyst)",
  architect: "var(--role-architect)",
  tester: "var(--role-tester)",
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
  // optional badges
  durationSeconds?: number | null;
  errorCount?: number;
  toolCallCount?: number;
}

function StepNodeComponent({ data, selected }: NodeProps<StepNodeData>) {
  const roleColor = ROLE_VAR[data.role] || "var(--content-muted)";
  const status = data.execStatus ?? "pending";

  const borderColor =
    status === "running"
      ? "var(--dag-running-border)"
      : status === "completed"
        ? "var(--dag-completed-border)"
        : status === "failed"
          ? "var(--dag-failed-border)"
          : selected
            ? "var(--status-selected)"
            : "var(--edge-default)";

  const bgColor =
    status === "running"
      ? "var(--dag-running-bg)"
      : status === "completed"
        ? "var(--dag-completed-bg)"
        : status === "failed"
          ? "var(--dag-failed-bg)"
          : "var(--surface-raised)";

  const labelColor =
    status === "running"
      ? "var(--dag-running-label)"
      : status === "completed"
        ? "var(--dag-completed-label)"
        : status === "failed"
          ? "var(--dag-failed-label)"
          : "var(--content-primary)";

  return (
    <div
      className="relative rounded-lg px-3 py-2.5"
      style={{
        background: bgColor,
        border: `${selected || status === "running" ? 2 : 1}px solid ${borderColor}`,
        minWidth: 160,
        maxWidth: 220,
        transition: "border-color 0.2s, background 0.2s",
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        style={{
          width: 10,
          height: 10,
          background: "var(--edge-strong)",
          borderColor: "var(--edge-default)",
        }}
      />

      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-body font-semibold truncate" style={{ color: labelColor }}>
          {data.label}
        </span>
        {status === "completed" && (
          <span className="shrink-0 text-meta" style={{ color: "var(--status-success)" }}>✓</span>
        )}
        {status === "failed" && (
          <span className="shrink-0 text-meta" style={{ color: "var(--status-error)" }}>✕</span>
        )}
      </div>

      {data.role && (
        <span
          className="mt-1 inline-block rounded-full px-2 py-0.5 font-mono text-[10px]"
          style={{
            backgroundColor: `color-mix(in srgb, ${roleColor} 14%, transparent)`,
            color: roleColor,
          }}
        >
          {data.role}
        </span>
      )}

      {data.assignment && (
        <div className="mt-1 truncate font-mono text-meta text-content-muted">
          {data.assignment}
        </div>
      )}

      {(data.durationSeconds != null || (data.errorCount ?? 0) > 0 || (data.toolCallCount ?? 0) > 0) && (
        <div className="mt-1.5 flex items-center gap-2 text-meta tabular-nums">
          {data.durationSeconds != null && data.durationSeconds >= 0 ? (
            <span className="text-content-muted">{formatStepDuration(data.durationSeconds)}</span>
          ) : null}
          {(data.errorCount ?? 0) > 0 ? (
            <span className="text-status-error">{data.errorCount} err</span>
          ) : null}
          {(data.toolCallCount ?? 0) > 0 ? (
            <span className="text-content-muted">{data.toolCallCount} calls</span>
          ) : null}
        </div>
      )}

      {status === "running" && (
        <div
          className="pointer-events-none absolute inset-0 rounded-lg"
          style={{
            border: "2px solid var(--dag-running-border)",
            animation: "pulse 1.5s ease-in-out infinite",
            opacity: 0.4,
          }}
        />
      )}

      <Handle
        type="source"
        position={Position.Right}
        style={{
          width: 10,
          height: 10,
          background: "var(--edge-strong)",
          borderColor: "var(--edge-default)",
        }}
      />
    </div>
  );
}

function formatStepDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m`;
}

export default memo(StepNodeComponent);
