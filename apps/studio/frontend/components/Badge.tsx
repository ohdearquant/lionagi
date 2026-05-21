import type { ReactNode } from "react";

export type BadgeTone = "ok" | "running" | "failed" | "pending" | "blocked" | "default";

export interface BadgeProps {
  children: ReactNode;
  tone?: BadgeTone;
  value?: string | null;
  className?: string;
}

const toneClasses: Record<BadgeTone, string> = {
  ok:      "border-status-success/40 bg-status-success-bg text-status-success",
  running: "border-status-running/40 bg-status-running-bg text-status-running",
  failed:  "border-status-error/40   bg-status-error-bg   text-status-error",
  pending: "border-status-warning/40 bg-status-warning-bg text-status-warning",
  blocked: "border-status-selected/40 bg-status-selected-bg text-status-selected",
  default: "border-edge bg-surface-overlay text-content-secondary",
};

function toneFromValue(value: string | null | undefined): BadgeTone {
  const normalized = value?.toLowerCase();

  if (
    normalized === "ok" ||
    normalized === "done" ||
    normalized === "success" ||
    normalized === "completed" ||
    normalized === "active"
  ) {
    return "ok";
  }

  if (normalized === "running" || normalized === "executing" || normalized === "open") {
    return "running";
  }

  if (normalized === "failed" || normalized === "failure" || normalized === "error") {
    return "failed";
  }

  if (
    normalized === "pending" ||
    normalized === "queued" ||
    normalized === "planned" ||
    normalized === "received"
  ) {
    return "pending";
  }

  if (normalized === "blocked" || normalized === "gated") {
    return "blocked";
  }

  return "default";
}

export default function Badge({ children, tone, value, className }: BadgeProps) {
  const resolvedTone =
    tone ?? toneFromValue(value ?? (typeof children === "string" ? children : null));

  return (
    <span
      className={[
        "inline-flex max-w-full items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide",
        toneClasses[resolvedTone],
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span className="truncate">{children}</span>
    </span>
  );
}
