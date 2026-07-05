/**
 * SchedulesBoard — true kanban board over time. Five columns side by side:
 * Upcoming (future days), Today (fires before midnight), Running (firings in
 * flight), Done (recent terminal firings), Paused (disabled schedules).
 *
 * Each column is an independent scrollable surface. The row scrolls
 * horizontally on narrow viewports rather than wrapping — board behavior.
 */
import { useState } from "react";
import { useTranslations } from "use-intl";
import Badge from "@/components/ui/Badge";
import type { ScheduleSummary } from "@/lib/types";
import { deriveLanes, latestRunBySchedule, type RunRow } from "./data";
import { RunCard, ScheduleCard } from "./cards";
import ScheduleDetailModal from "./ScheduleDetailModal";

/** Dot indicator in the lane header — shows live status at a glance. */
function HeaderDot({ color, pulse }: { color: string; pulse?: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={["h-2 w-2 shrink-0 rounded-full", pulse ? "animate-pulse" : ""].join(" ")}
      style={{ background: color }}
    />
  );
}

/**
 * Thin wrapper that adds card-shadow elevation to each card without touching
 * the card component itself (cards.tsx is read-only).
 */
function CardShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="shadow-card hover:shadow-card-hover rounded-md transition-shadow duration-150">
      {children}
    </div>
  );
}

function Lane({
  label,
  count,
  dot,
  pulse,
  emptyText,
  children,
}: {
  label: string;
  count: number;
  dot: string;
  pulse?: boolean;
  emptyText: string;
  children: React.ReactNode;
}) {
  return (
    <section
      aria-label={label}
      className="flex min-w-[240px] flex-1 flex-col rounded-lg border border-edge bg-surface-raised shadow-card"
    >
      {/* Pinned column header */}
      <header className="flex shrink-0 items-center gap-2 rounded-t-lg border-b border-edge px-3 py-2.5">
        <HeaderDot color={dot} pulse={pulse} />
        <span className="ui-uppercase font-ui text-[length:var(--t-xs)] font-semibold text-content-secondary">
          {label}
        </span>
        <Badge tone="default" className="ml-auto tabular-nums">
          {count}
        </Badge>
      </header>

      {/* Independently scrollable card body */}
      <div className="flex flex-1 flex-col gap-2 overflow-y-auto p-2.5">
        {count === 0 ? (
          <p className="py-8 text-center text-meta text-content-muted">{emptyText}</p>
        ) : (
          children
        )}
      </div>
    </section>
  );
}

export default function SchedulesBoard({
  schedules,
  runs,
  nowMs,
  onChanged,
  initialSelectedId,
}: {
  schedules: ScheduleSummary[];
  runs: RunRow[];
  nowMs: number;
  onChanged: () => void;
  /** Deep-link target: opens this schedule's detail on mount. */
  initialSelectedId?: string | null;
}) {
  const t = useTranslations("schedules.lanes");
  const lanes = deriveLanes(schedules, runs, nowMs);
  const lastRuns = latestRunBySchedule(runs);
  const [selectedScheduleId, setSelectedScheduleId] = useState<string | null>(
    initialSelectedId ?? null,
  );

  return (
    <>
      <div className="flex min-h-0 flex-1 gap-3 overflow-x-auto px-6 pb-6 pt-1">
        {/* Upcoming — enabled schedules firing on future days */}
        <Lane
          label={t("upcoming")}
          count={lanes.upcoming.length}
          dot="var(--content-muted)"
          emptyText={t("emptyUpcoming")}
        >
          {lanes.upcoming.map((s) => (
            <CardShell key={s.id}>
              <ScheduleCard
                schedule={s}
                lane="upcoming"
                lastRun={lastRuns.get(s.id)}
                nowMs={nowMs}
                onChanged={onChanged}
                onOpen={setSelectedScheduleId}
              />
            </CardShell>
          ))}
        </Lane>

        {/* Today — enabled schedules firing before midnight */}
        <Lane
          label={t("today")}
          count={lanes.today.length}
          dot="var(--accent)"
          emptyText={t("emptyToday")}
        >
          {lanes.today.map((s) => (
            <CardShell key={s.id}>
              <ScheduleCard
                schedule={s}
                lane="today"
                lastRun={lastRuns.get(s.id)}
                nowMs={nowMs}
                onChanged={onChanged}
                onOpen={setSelectedScheduleId}
              />
            </CardShell>
          ))}
        </Lane>

        {/* Running — firings currently in flight (live-pulse dot) */}
        <Lane
          label={t("running")}
          count={lanes.running.length}
          dot="var(--status-running)"
          pulse={lanes.running.length > 0}
          emptyText={t("emptyRunning")}
        >
          {lanes.running.map((r) => (
            <CardShell key={r.id}>
              <RunCard run={r} nowMs={nowMs} />
            </CardShell>
          ))}
        </Lane>

        {/* Done — recent terminal firings */}
        <Lane
          label={t("done")}
          count={lanes.done.length}
          dot="var(--status-success)"
          emptyText={t("emptyDone")}
        >
          {lanes.done.map((r) => (
            <CardShell key={r.id}>
              <RunCard run={r} nowMs={nowMs} />
            </CardShell>
          ))}
        </Lane>

        {/* Paused — disabled schedules; own column keeps them reachable */}
        <Lane
          label={t("paused")}
          count={lanes.paused.length}
          dot="var(--content-muted)"
          emptyText={t("emptyUpcoming")}
        >
          {lanes.paused.map((s) => (
            <CardShell key={s.id}>
              <ScheduleCard
                schedule={s}
                lane="paused"
                lastRun={lastRuns.get(s.id)}
                nowMs={nowMs}
                onChanged={onChanged}
                onOpen={setSelectedScheduleId}
              />
            </CardShell>
          ))}
        </Lane>
      </div>
      {selectedScheduleId && (
        <ScheduleDetailModal
          scheduleId={selectedScheduleId}
          onClose={() => setSelectedScheduleId(null)}
          onChanged={onChanged}
        />
      )}
    </>
  );
}
