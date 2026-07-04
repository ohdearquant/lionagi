import type { ReactNode } from "react";

export type MetricTone = "neutral" | "ok" | "running" | "failed" | "pending";

export interface MetricCardProps {
  label: string;
  value: ReactNode;
  // Small line beneath the value (e.g. "24h", "from 18 runs")
  hint?: ReactNode;
  // Delta to render to the right of the value (e.g. "+3", "-2%")
  delta?: { text: ReactNode; tone?: "up" | "down" | "neutral" };
  tone?: MetricTone;
  // When set, the entire card becomes clickable.
  onClick?: () => void;
  // When provided, a small icon character ("▲", "●") renders next to the label.
  icon?: ReactNode;
  className?: string;
}

const TONE_BORDER: Record<MetricTone, string> = {
  neutral: "border-edge",
  ok: "border-status-success/30",
  running: "border-status-running/40",
  failed: "border-status-failure/40",
  pending: "border-status-pending/40",
};

const TONE_VALUE_TEXT: Record<MetricTone, string> = {
  neutral: "text-content-primary",
  ok: "text-status-success",
  running: "text-status-running",
  failed: "text-status-failure",
  pending: "text-status-pending",
};

const DELTA_CLASS: Record<NonNullable<MetricCardProps["delta"]>["tone"] & string, string> = {
  up: "text-status-success",
  down: "text-status-failure",
  neutral: "text-content-muted",
};

export default function MetricCard({
  label,
  value,
  hint,
  delta,
  tone = "neutral",
  onClick,
  icon,
  className,
}: MetricCardProps) {
  const interactive = !!onClick;

  const base = "min-w-0 rounded border bg-surface-raised p-4 transition-all duration-150 text-left";
  const interactiveCls = interactive
    ? "hover:border-edge-strong cursor-pointer focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-1 focus:ring-offset-surface-base"
    : "";

  const content = (
    <>
      <div className="flex items-center gap-1.5 text-meta uppercase tracking-[0.06em] text-content-muted">
        {icon ? <span className="text-[10px] leading-none">{icon}</span> : null}
        <span className="truncate">{label}</span>
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <div
          className={`text-2xl font-semibold tabular-nums tracking-tight ${TONE_VALUE_TEXT[tone]}`}
        >
          {value}
        </div>
        {delta ? (
          <div className={`text-meta tabular-nums ${DELTA_CLASS[delta.tone ?? "neutral"]}`}>
            {delta.text}
          </div>
        ) : null}
      </div>
      {hint ? <div className="mt-1 text-meta text-content-muted truncate">{hint}</div> : null}
    </>
  );

  if (interactive) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={[base, TONE_BORDER[tone], interactiveCls, className].filter(Boolean).join(" ")}
      >
        {content}
      </button>
    );
  }
  return (
    <section className={[base, TONE_BORDER[tone], className].filter(Boolean).join(" ")}>
      {content}
    </section>
  );
}
