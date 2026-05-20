import type { ReactNode } from "react";

export type BadgeTone = "ok" | "running" | "failed" | "pending" | "blocked" | "default";

export interface BadgeProps {
  children: ReactNode;
  tone?: BadgeTone;
  value?: string | null;
  className?: string;
}

const toneClasses: Record<BadgeTone, string> = {
  ok: "border-emerald-800 bg-emerald-950/40 text-emerald-300",
  running: "border-blue-800 bg-blue-950/40 text-blue-300",
  failed: "border-red-800 bg-red-950/40 text-red-300",
  pending: "border-amber-800 bg-amber-950/40 text-amber-300",
  blocked: "border-fuchsia-800 bg-fuchsia-950/40 text-fuchsia-300",
  default: "border-neutral-800 bg-neutral-950 text-neutral-300",
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
        "inline-flex max-w-full items-center rounded-full border px-2 py-0.5 text-xs font-medium",
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
