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
        "min-w-0 rounded border border-edge bg-surface-raised p-4 shadow-card transition-all duration-150 hover:border-edge-strong hover:shadow-card-hover",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="truncate text-meta uppercase tracking-[0.08em] text-content-muted">
        {label}
      </div>
      <div className="mt-3 truncate text-3xl font-semibold tabular-nums tracking-tight text-content-primary">
        {value}
      </div>
      {delta !== undefined && delta !== null ? (
        <div className={`mt-1.5 truncate text-body ${deltaClasses[deltaTone]}`}>{delta}</div>
      ) : null}
    </section>
  );
}
