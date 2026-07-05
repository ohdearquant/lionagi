export interface StatCardProps {
  /** Short uppercase label above the value. */
  label: string;
  /** Display value — already formatted as a string by the caller. */
  value: string;
  /** Optional smaller line below the value. */
  sub?: string;
  /** Color tone applied to the value text. */
  tone?: "ok" | "error";
  className?: string;
}

/**
 * Small metric tile: uppercase label, large mono value, optional sub-line.
 * Used in overview panels and stat grids throughout the run/step detail views.
 */
export default function StatCard({ label, value, sub, tone, className }: StatCardProps) {
  return (
    <div
      className={["rounded border border-edge bg-surface-raised px-3 py-2", className]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="text-[length:var(--t-xs)] uppercase tracking-wider text-content-muted">
        {label}
      </div>
      <div
        className={[
          "mt-0.5 font-mono text-base font-semibold",
          tone === "error"
            ? "text-status-error"
            : tone === "ok"
              ? "text-status-success"
              : "text-content-primary",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {value}
      </div>
      {sub && <div className="text-meta text-content-muted">{sub}</div>}
    </div>
  );
}
