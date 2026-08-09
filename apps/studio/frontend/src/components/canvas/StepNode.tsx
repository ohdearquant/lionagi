"use client";

import { memo, useEffect, useState } from "react";
import { Handle, Position } from "reactflow";
import type { NodeProps } from "reactflow";
import { useTranslations } from "use-intl";
import { IconCheck, IconClose, IconPause, IconWarning } from "@/components/ui/icons";
import { NODE_HEIGHT, NODE_WIDTH } from "./useLayout";

// What the bottom-right corner says before there is a duration to put there.
// "pending" has no lifecycle signal to report yet, so its placeholder is a
// language-neutral dash rather than a translated word.
const STATUS_WORD_KEY: Record<Exclude<NodeExecStatus, "pending">, string> = {
  queued: "graphNodeStatusQueued",
  running: "graphNodeStatusRunning",
  awaiting_approval: "graphNodeStatusApproval",
  paused: "graphNodeStatusPaused",
  completed: "graphNodeStatusDone",
  failed: "graphNodeStatusFailed",
  escalated: "graphNodeStatusEscalated",
};

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
  | "pending"
  | "queued"
  | "running"
  | "awaiting_approval"
  | "paused"
  | "completed"
  | "failed"
  | "escalated";

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

// Non-animation precedence cues (border weight + a left-edge status rail)
// that must remain readable at the minimum fit zoom (0.1), where the pulse
// ring and label text are effectively invisible. running is strongest (3px
// border, brightest rail), completed/failed/warn are moderate and mutually
// distinguishable by color alone (2px, distinct rail hue), pending/queued
// recede (1px, no rail) so they never compete with completed work for
// attention. Exported so StepNode.test.ts can assert the precedence
// contract without mounting React Flow.
export interface NodeVisualStyle {
  borderWidth: number;
  borderColor: string;
  bgColor: string;
  labelColor: string;
  railColor: string;
}

export function computeNodeVisualStyle(status: NodeExecStatus, selected: boolean): NodeVisualStyle {
  const isTerminalError = status === "failed";
  const isWarn = status === "awaiting_approval" || status === "paused" || status === "escalated";

  const borderColor =
    status === "running"
      ? "var(--dag-running-border)"
      : status === "completed"
        ? "var(--dag-completed-border)"
        : isTerminalError
          ? "var(--dag-failed-border)"
          : isWarn
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
          : isWarn
            ? "var(--dag-warn-bg)"
            : "var(--dag-pending-bg)";

  const labelColor =
    status === "running"
      ? "var(--dag-running-label)"
      : status === "completed"
        ? "var(--dag-completed-label)"
        : isTerminalError
          ? "var(--dag-failed-label)"
          : isWarn
            ? "var(--dag-warn-label)"
            : "var(--content-primary)";

  const borderWidth =
    status === "running" ? 3 : status === "completed" || isTerminalError || isWarn ? 2 : 1;

  const railColor =
    status === "running"
      ? "var(--dag-running-border)"
      : status === "completed"
        ? "var(--dag-completed-border)"
        : isTerminalError
          ? "var(--dag-failed-border)"
          : isWarn
            ? "var(--dag-warn-border)"
            : "transparent";

  return { borderWidth, borderColor, bgColor, labelColor, railColor };
}

function StepNodeComponent({ data, selected }: NodeProps<StepNodeData>) {
  const t = useTranslations("history.detail");
  // roleColor arrives as a data-driven CSS var string — keep inline
  const roleColor = ROLE_VAR[data.role] || "var(--content-muted)";
  const status = data.execStatus ?? "pending";
  const reducedMotion = usePrefersReducedMotion();

  // These derive from status data (dag-* tokens) — keep inline
  const visual = computeNodeVisualStyle(status, !!selected);
  const borderWidth = selected ? Math.max(visual.borderWidth, 2) : visual.borderWidth;

  // The bottom-right corner always says something. Elapsed time once there is
  // any, the status word before that. A corner that can be empty makes the
  // card change shape as a run progresses, and a reader who has to re-find a
  // field has stopped reading the graph at a glance.
  const magnitude =
    data.durationSeconds != null && data.durationSeconds >= 0
      ? formatStepDuration(data.durationSeconds)
      : status === "pending"
        ? "—"
        : t(STATUS_WORD_KEY[status]);

  return (
    <div
      className="relative flex flex-col justify-between rounded-md px-2.5 py-2"
      style={{
        background: visual.bgColor,
        border: `${borderWidth}px solid ${visual.borderColor}`,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        boxShadow:
          status === "running"
            ? "0 0 0 3px color-mix(in srgb, var(--dag-running-border) 18%, transparent)"
            : selected
              ? "0 0 0 2px color-mix(in srgb, var(--status-selected) 22%, transparent)"
              : "0 1px 3px rgba(0,0,0,0.12)",
        transition: "border-color 0.15s, background 0.15s, box-shadow 0.15s",
      }}
    >
      {/* Status rail — a left-edge color bar that survives the readability
          zoom floor even when the card is too small to read text or icons.
          A span, not a div: the card's rows() test selects direct div
          children of this card as its two content rows, and the rail is
          decorative chrome, not a third row. */}
      <span
        className="pointer-events-none absolute inset-y-0 left-0 block rounded-l-md"
        style={{ width: 3, background: visual.railColor }}
      />

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

      {/* Top row: what this step is, and what state it is in. */}
      <div className="flex items-start justify-between gap-1.5">
        <span
          className="truncate font-mono text-[length:var(--t-sm)] font-semibold leading-snug"
          style={{ color: visual.labelColor }}
        >
          {data.label}
        </span>
        {(data.errorCount ?? 0) > 0 && (
          <span className="shrink-0 font-mono text-[length:var(--t-xs)] tabular-nums leading-snug text-status-error">
            {data.errorCount}
          </span>
        )}
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
          <span className="flex shrink-0 items-center text-status-warning">
            <IconWarning size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "awaiting_approval" && (
          <span className="flex shrink-0 items-center text-status-warning">
            <IconWarning size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "paused" && (
          <span className="flex shrink-0 items-center text-status-warning">
            <IconPause size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "running" && (
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full${reducedMotion ? "" : " animate-pulse"}`}
            style={{ background: "var(--dag-running-border)" }}
          />
        )}
      </div>

      {/* Bottom row: what kind of step it is, and how much of it there has
          been so far. Both corners are fixed, so the same fact is always in
          the same place on every card at every zoom. The assignment and the
          tool-call count moved to the panel: they are what you read about one
          node you have already picked, not what you scan a graph for. */}
      <div className="flex items-end justify-between gap-1.5">
        <span
          className="truncate font-mono text-[length:var(--t-xs)] uppercase leading-tight tracking-wide"
          style={{ color: data.role ? roleColor : "transparent" }}
        >
          {data.role || "."}
        </span>
        <span className="shrink-0 font-mono text-[length:var(--t-xs)] tabular-nums leading-tight text-content-muted">
          {magnitude}
        </span>
      </div>

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
