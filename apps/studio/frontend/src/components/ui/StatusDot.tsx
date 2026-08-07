export interface StatusDotProps {
  /** Raw status string — used to derive color and whether the dot pulses. */
  status: string;
  className?: string;
}

const TERMINAL_STATUSES = new Set([
  "completed",
  "done",
  "success",
  "finished",
  "running_complete",
  "director-managed-complete",
  "failed",
  "failure",
  "error",
  "cancelled",
  "timed_out",
]);

// Process-dead-but-non-terminal health states, plus "unknown" (liveness
// genuinely undetermined, e.g. no evidence has landed yet): muted and
// static either way — neither a dead process nor an undetermined one may
// pulse as if confirmed alive.
const STALE_STATUSES = new Set(["stale", "orphaned", "zombie", "unresponsive", "unknown"]);

function statusColor(status: string): string {
  const s = status.toLowerCase();
  if (s === "failed" || s === "error" || s === "failure" || s === "timed_out")
    return "var(--status-failure)";
  if (s === "cancelled" || STALE_STATUSES.has(s)) return "var(--content-muted)";
  if (s === "gated" || s === "needs_review" || s === "blocked") return "var(--status-pending)";
  if (s === "completed" || s === "done" || s === "success") return "var(--status-success)";
  return "var(--status-running)";
}

/**
 * Small circular status indicator. Pulses via `live-pulse-dot` CSS animation
 * when the status is non-terminal; static for terminal and stale states.
 */
export default function StatusDot({ status, className }: StatusDotProps) {
  const color = statusColor(status);
  const s = status.toLowerCase();
  const isTerminal = TERMINAL_STATUSES.has(s) || STALE_STATUSES.has(s);
  return (
    <span
      aria-hidden="true"
      className={[
        "inline-block h-1.5 w-1.5 shrink-0 rounded-full",
        isTerminal ? null : "live-pulse-dot",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      style={{ background: color }}
    />
  );
}
