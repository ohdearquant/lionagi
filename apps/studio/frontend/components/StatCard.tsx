import type { ReactNode } from "react";

export type StatDeltaTone = "neutral" | "up" | "down";

export interface StatCardProps {
  label: string;
  value: ReactNode;
  delta?: ReactNode;
  deltaTone?: StatDeltaTone;
  className?: string;
}

const deltaClasses: Record<StatDeltaTone, string> = {
  neutral: "text-content-muted",
  up: "text-status-success",
  down: "text-status-error",
};

export default function StatCard({
  label,
  value,
  delta,
  deltaTone = "neutral",
  className,
}: StatCardProps) {
  return (
    <section
      className={[
        "min-w-0 rounded border border-edge bg-surface-raised p-4 transition-colors hover:border-edge-strong",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="truncate text-meta uppercase tracking-[0.08em] text-content-muted">
        {label}
      </div>
      <div className="mt-3 truncate text-3xl font-semibold text-content-primary">{value}</div>
      {delta !== undefined && delta !== null ? (
        <div className={`mt-2 truncate text-body ${deltaClasses[deltaTone]}`}>{delta}</div>
      ) : null}
    </section>
  );
}
