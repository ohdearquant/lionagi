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
  neutral: "text-neutral-500",
  up: "text-emerald-300",
  down: "text-red-300",
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
      className={["min-w-0 rounded border border-neutral-800 bg-neutral-950 p-4", className]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="truncate text-xs uppercase tracking-normal text-neutral-500">{label}</div>
      <div className="mt-3 truncate text-3xl font-semibold text-neutral-200">{value}</div>
      {delta !== undefined && delta !== null ? (
        <div className={`mt-2 truncate text-sm ${deltaClasses[deltaTone]}`}>{delta}</div>
      ) : null}
    </section>
  );
}
