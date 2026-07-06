/**
 * ScheduleCards — one card per standing automation. Cards read as objects you
 * manage (name, what triggers it, how it last ran, when it fires next) rather
 * than rows in a grid. A disabled schedule shows "Paused" where a live one
 * shows its next firing — a stopped schedule never advertises a next fire.
 */
import { useMemo, useState } from "react";
import { useLocale, useTranslations } from "use-intl";
import StatusPill from "@/components/ui/StatusPill";
import IconButton from "@/components/ui/IconButton";
import { IconPause, IconPencil, IconSchedule } from "@/components/ui/icons";
import { triggerSchedule } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import type { ScheduleSummary } from "@/lib/types";
import EnabledToggle from "./EnabledToggle";
import { classifyError } from "./errorClassify";
import { humanTrigger } from "./trigger";
import {
  KNOWN_RUN_STATUSES,
  formatDelta,
  latestRunBySchedule,
  nextFireState,
  sortSchedulesForCards,
  toMs,
  type RunRow,
} from "./data";

function NextFire({ schedule, nowMs }: { schedule: ScheduleSummary; nowMs: number }) {
  const t = useTranslations("schedules");
  const locale = useLocale();
  const state = nextFireState(schedule, nowMs);

  if (state.kind === "paused") {
    return (
      <span className="inline-flex items-center gap-1 text-meta text-content-muted">
        <IconPause size={11} strokeWidth={2} />
        {t("card.paused")}
      </span>
    );
  }
  if (state.kind === "watching") {
    return <span className="text-meta text-content-secondary">{t("card.watching")}</span>;
  }
  if (state.kind === "unscheduled") {
    return <span className="text-meta text-content-muted">{t("card.notScheduled")}</span>;
  }

  const absolute = new Date(state.fireMs).toLocaleString(locale, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return (
    <span
      className="inline-flex items-center gap-1 text-meta"
      style={{
        color: state.overdue
          ? "var(--status-warning)"
          : state.soon
            ? "var(--accent)"
            : "var(--content-secondary)",
      }}
    >
      <IconSchedule size={11} strokeWidth={2} />
      <span className="font-data text-content-primary">{absolute}</span>
      <span>
        {state.overdue
          ? t("card.overdue", { delta: formatDelta(-state.deltaMs) })
          : t("card.in", { delta: formatDelta(state.deltaMs) })}
      </span>
    </span>
  );
}

function LastRun({ run, nowMs }: { run: RunRow | undefined; nowMs: number }) {
  const t = useTranslations("schedules");
  const tError = useTranslations("schedules.error");
  const tStatus = useTranslations("history.status");

  if (!run) return <span className="text-meta text-content-muted">{t("table.neverRun")}</span>;

  const label = KNOWN_RUN_STATUSES.has(run.status)
    ? tStatus(run.status as Parameters<typeof tStatus>[0])
    : undefined;
  const errorLine = run.status === "failed" ? classifyError(run.error_detail, tError) : null;

  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <div className="flex items-center gap-1.5">
        <StatusPill value={run.status} taxonomy="session" label={label} />
        <span className="text-meta text-content-muted">
          {formatDelta(nowMs - toMs(run.fired_at))}
          {t("detail.ago")}
        </span>
      </div>
      {errorLine && (
        <span className="truncate text-meta text-status-error" title={errorLine}>
          {errorLine}
        </span>
      )}
    </div>
  );
}

function ScheduleCard({
  schedule,
  lastRun,
  nowMs,
  onChanged,
  onOpen,
}: {
  schedule: ScheduleSummary;
  lastRun: RunRow | undefined;
  nowMs: number;
  onChanged: () => void;
  onOpen: (id: string) => void;
}) {
  const t = useTranslations("schedules");
  const locale = useLocale();
  const { toast } = useToast();
  const [triggering, setTriggering] = useState(false);

  const trigger = humanTrigger(schedule, t, locale);

  async function handleTrigger(e: React.MouseEvent) {
    e.stopPropagation();
    setTriggering(true);
    try {
      const res = await triggerSchedule(schedule.id);
      toast(t("card.runStarted", { id: res.run_id.slice(0, 8) }), "success");
      onChanged();
    } catch {
      toast(t("card.triggerFailed"), "error");
    } finally {
      setTriggering(false);
    }
  }

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
    <div
      onClick={() => onOpen(schedule.id)}
      className={[
        "shadow-card hover:shadow-card-hover group flex cursor-pointer flex-col gap-3 rounded-lg border border-edge bg-surface-raised p-4 transition-shadow duration-150",
        schedule.enabled ? "" : "opacity-70",
      ].join(" ")}
    >
      {/* Name + enabled toggle */}
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p
            className="truncate font-data text-body font-semibold text-content-primary"
            title={schedule.name}
          >
            {schedule.name}
          </p>
          {schedule.description && (
            <p
              className="mt-0.5 line-clamp-2 text-meta text-content-muted"
              title={schedule.description}
            >
              {schedule.description}
            </p>
          )}
        </div>
        {/* EnabledToggle stops click propagation itself, so toggling never opens the card. */}
        <EnabledToggle
          scheduleId={schedule.id}
          enabled={Boolean(schedule.enabled)}
          onToggled={onChanged}
        />
      </div>

      {/* Trigger */}
      <div className="flex items-center gap-1.5 text-meta text-content-secondary">
        <IconSchedule size={12} strokeWidth={2} className="shrink-0 text-content-muted" />
        <span className="truncate font-data" title={trigger.title}>
          {trigger.text}
        </span>
      </div>

      {/* Last run */}
      <LastRun run={lastRun} nowMs={nowMs} />

      {/* Footer: next fire + actions */}
      <div className="mt-auto flex items-center justify-between gap-2 border-t border-edge pt-3">
        <NextFire schedule={schedule} nowMs={nowMs} />
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={triggering}
            onClick={(e) => void handleTrigger(e)}
            className="shrink-0 rounded border border-edge px-2 py-0.5 text-meta text-content-secondary transition-colors duration-100 hover:border-edge-strong hover:text-content-primary disabled:opacity-50"
          >
            {triggering ? t("card.triggering") : t("card.runNow")}
          </button>
          <IconButton
            aria-label={t("table.editAction")}
            title={t("table.editAction")}
            onClick={(e) => {
              e.stopPropagation();
              onOpen(schedule.id);
            }}
          >
            <IconPencil size={13} strokeWidth={2} />
          </IconButton>
        </div>
      </div>
    </div>
  );
}

export default function ScheduleCards({
  schedules,
  runs,
  nowMs,
  onChanged,
  onOpen,
}: {
  schedules: ScheduleSummary[];
  runs: RunRow[];
  nowMs: number;
  onChanged: () => void;
  onOpen: (id: string) => void;
}) {
  const lastRuns = useMemo(() => latestRunBySchedule(runs), [runs]);
  const sorted = useMemo(() => sortSchedulesForCards(schedules), [schedules]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6">
      <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]">
        {sorted.map((s) => (
          <ScheduleCard
            key={s.id}
            schedule={s}
            lastRun={lastRuns.get(s.id)}
            nowMs={nowMs}
            onChanged={onChanged}
            onOpen={onOpen}
          />
        ))}
      </div>
    </div>
  );
}
