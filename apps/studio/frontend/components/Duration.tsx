import type { ReactNode } from "react";

export interface DurationProps {
  // Accepts seconds or milliseconds. Heuristic: > 1e6 is ms.
  value: number | null | undefined;
  // When set, fallback rendering for null/negative cases.
  fallback?: ReactNode;
  className?: string;
}

function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m - h * 60;
  return mm > 0 ? `${h}h ${mm}m` : `${h}h`;
}

export default function Duration({ value, fallback, className }: DurationProps) {
  const cls = ["tabular-nums", className].filter(Boolean).join(" ");

  if (value == null || Number.isNaN(value)) {
    return (
      <span className={[cls, "text-content-muted"].join(" ")} title="duration unavailable">
        {fallback ?? "—"}
      </span>
    );
  }

  // Negative durations indicate a timing computation bug. Render a dash
  // with explanatory tooltip rather than expose the broken number.
  if (value < 0) {
    return (
      <span
        className={[cls, "text-content-muted"].join(" ")}
        title="timestamp missing or out of order"
      >
        {fallback ?? "—"}
      </span>
    );
  }

  // Heuristic: values > 1e6 are almost certainly milliseconds. We treat
  // smaller numbers as seconds.
  const seconds = value > 1e6 ? value / 1000 : value;
  return <span className={cls}>{formatDuration(seconds)}</span>;
}
