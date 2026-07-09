"use client";

import { memo, useEffect, useState } from "react";
import { Handle, Position } from "reactflow";
import type { NodeProps } from "reactflow";
import { IconCheck, IconClose, IconWarning } from "@/components/ui/icons";

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR hydration guard: window.matchMedia unavailable during server render
    setReduced(mq.matches);
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return reduced;
}

const ROLE_VAR: Record<string, string> = {
  researcher: "var(--role-researcher)",
  implementer: "var(--role-implementer)",
  reviewer: "var(--role-reviewer)",
  critic: "var(--role-critic)",
  analyst: "var(--role-analyst)",
  architect: "var(--role-architect)",
  tester: "var(--role-tester)",
};

// "pending" = no lifecycle signal observed at all (never queued); "queued" =
// an explicit NodeQueued signal was seen but execution has not started. Both
// render as the same neutral card — the distinction matters for correctness
// (a queued node must never be painted as running), not for a separate look.
export type NodeExecStatus =
  "pending" | "queued" | "running" | "awaiting_approval" | "completed" | "failed" | "escalated";

export interface StepNodeData {
  label: string;
  role: string;
  assignment: string;
  prompt: string;
  capacity: number;
  timeout: number | null;
  inputs: string[];
  outputs: string[];
  execStatus?: NodeExecStatus;
  // optional badges
  durationSeconds?: number | null;
  errorCount?: number;
  toolCallCount?: number;
}

function StepNodeComponent({ data, selected }: NodeProps<StepNodeData>) {
  // roleColor arrives as a data-driven CSS var string — keep inline
  const roleColor = ROLE_VAR[data.role] || "var(--content-muted)";
  const status = data.execStatus ?? "pending";
  const reducedMotion = usePrefersReducedMotion();
  const isTerminalError = status === "failed" || status === "escalated";

  // These colors derive from status data (dag-* tokens) — keep inline
  const borderColor =
    status === "running"
      ? "var(--dag-running-border)"
      : status === "completed"
        ? "var(--dag-completed-border)"
        : isTerminalError
          ? "var(--dag-failed-border)"
          : status === "awaiting_approval"
            ? "var(--dag-warn-border)"
            : selected
              ? "var(--status-selected)"
              : "var(--dag-pending-border)";

  const bgColor =
    status === "running"
      ? "var(--dag-running-bg)"
      : status === "completed"
        ? "var(--dag-completed-bg)"
        : isTerminalError
          ? "var(--dag-failed-bg)"
          : status === "awaiting_approval"
            ? "var(--dag-warn-bg)"
            : "var(--dag-pending-bg)";

  const labelColor =
    status === "running"
      ? "var(--dag-running-label)"
      : status === "completed"
        ? "var(--dag-completed-label)"
        : isTerminalError
          ? "var(--dag-failed-label)"
          : status === "awaiting_approval"
            ? "var(--dag-warn-label)"
            : "var(--content-primary)";

  return (
    <div
      className="relative rounded-md px-2.5 py-2"
      style={{
        background: bgColor,
        border: `${selected || status === "running" ? 2 : 1}px solid ${borderColor}`,
        minWidth: 148,
        maxWidth: 210,
        boxShadow:
          status === "running"
            ? "0 0 0 3px color-mix(in srgb, var(--dag-running-border) 18%, transparent)"
            : selected
              ? "0 0 0 2px color-mix(in srgb, var(--status-selected) 22%, transparent)"
              : "0 1px 3px rgba(0,0,0,0.12)",
        transition: "border-color 0.15s, background 0.15s, box-shadow 0.15s",
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        style={{
          width: 8,
          height: 8,
          background: "var(--edge-default)",
          borderColor: "var(--surface-raised)",
          borderWidth: 1.5,
        }}
      />

      <div className="flex items-center justify-between gap-1.5">
        <span
          className="truncate font-mono text-[length:var(--t-xs)] font-semibold leading-snug"
          style={{ color: labelColor }}
        >
          {data.label}
        </span>
        {status === "completed" && (
          <span className="flex shrink-0 items-center text-status-success">
            <IconCheck size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "failed" && (
          <span className="flex shrink-0 items-center text-status-error">
            <IconClose size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "escalated" && (
          <span className="flex shrink-0 items-center text-status-error">
            <IconWarning size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "awaiting_approval" && (
          <span className="flex shrink-0 items-center text-status-warning">
            <IconWarning size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "running" && (
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full${reducedMotion ? "" : " animate-pulse"}`}
            style={{ background: "var(--dag-running-border)" }}
          />
        )}
      </div>

      {data.role && (
        <span
          className="mt-1 inline-block rounded px-1.5 py-px font-mono text-[length:var(--t-xs)] leading-tight tracking-wide"
          style={{
            backgroundColor: `color-mix(in srgb, ${roleColor} 14%, transparent)`,
            color: roleColor,
          }}
        >
          {data.role}
        </span>
      )}

      {data.assignment && (
        <div className="mt-0.5 truncate font-mono text-[length:var(--t-xs)] leading-snug text-content-muted">
          {data.assignment}
        </div>
      )}

      {(data.durationSeconds != null ||
        (data.errorCount ?? 0) > 0 ||
        (data.toolCallCount ?? 0) > 0) && (
        <div className="mt-1 flex items-center gap-2 font-mono text-[length:var(--t-xs)] tabular-nums text-content-muted">
          {data.durationSeconds != null && data.durationSeconds >= 0 ? (
            <span>{formatStepDuration(data.durationSeconds)}</span>
          ) : null}
          {(data.errorCount ?? 0) > 0 ? (
            <span className="text-status-error">{data.errorCount} err</span>
          ) : null}
          {(data.toolCallCount ?? 0) > 0 ? <span>{data.toolCallCount} calls</span> : null}
        </div>
      )}

      {status === "running" && (
        <div
          className="pointer-events-none absolute inset-0 rounded-md opacity-35"
          style={{
            border: "2px solid var(--dag-running-border)",
            animation: reducedMotion ? "none" : "pulse 1.5s ease-in-out infinite",
          }}
        />
      )}

      <Handle
        type="source"
        position={Position.Right}
        style={{
          width: 8,
          height: 8,
          background: "var(--edge-default)",
          borderColor: "var(--surface-raised)",
          borderWidth: 1.5,
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
