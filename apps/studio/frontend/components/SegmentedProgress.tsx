export interface SegmentedProgressProps {
  // Each segment is a count for a given status; rendered proportional to
  // total. Order is fixed: completed, failed, running, blocked, pending.
  completed?: number;
  failed?: number;
  running?: number;
  blocked?: number;
  pending?: number;
  className?: string;
  // When set, draws a label like "9 / 12" centered above the bar.
  showCount?: boolean;
}

export default function SegmentedProgress({
  completed = 0,
  failed = 0,
  running = 0,
  blocked = 0,
  pending = 0,
  className,
  showCount = false,
}: SegmentedProgressProps) {
  const total = Math.max(1, completed + failed + running + blocked + pending);
  const done = completed + failed + blocked;

  const segments: Array<{ key: string; count: number; color: string; title: string }> = [
    { key: "completed", count: completed, color: "var(--status-success)", title: "completed" },
    { key: "failed", count: failed, color: "var(--status-error)", title: "failed" },
    { key: "running", count: running, color: "var(--status-running)", title: "running" },
    { key: "blocked", count: blocked, color: "var(--status-selected)", title: "blocked" },
    { key: "pending", count: pending, color: "var(--surface-overlay)", title: "pending" },
  ].filter((s) => s.count > 0);

  return (
    <div className={["w-full", className].filter(Boolean).join(" ")}>
      {showCount ? (
        <div className="mb-1 flex items-baseline justify-between text-meta text-content-muted">
          <span className="uppercase tracking-[0.06em]">Progress</span>
          <span className="tabular-nums text-content-secondary">
            {done} / {completed + failed + running + blocked + pending}
          </span>
        </div>
      ) : null}
      <div
        className="flex h-1.5 w-full overflow-hidden rounded-full bg-surface-overlay"
        role="progressbar"
        aria-valuenow={done}
        aria-valuemin={0}
        aria-valuemax={total}
      >
        {segments.map((s) => (
          <div
            key={s.key}
            title={`${s.title}: ${s.count}`}
            style={{
              width: `${(s.count / total) * 100}%`,
              backgroundColor: s.color,
            }}
            className="h-full"
          />
        ))}
      </div>
    </div>
  );
}
