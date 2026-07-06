/**
 * SchedulesTable — one row per schedule. Replaces the kanban board: schedules
 * are standing automations that don't move through columns, so a sortable
 * table (name, trigger, enabled, last run, next fire, actions) is the
 * functional view. Run history lives on the detail page, not here.
 */
import { useMemo, useState } from "react";
import { useLocale, useTranslations } from "use-intl";
import StatusPill from "@/components/ui/StatusPill";
import IconButton from "@/components/ui/IconButton";
import { IconChevronDown, IconChevronUp, IconPencil } from "@/components/ui/icons";
import { triggerSchedule } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import type { ScheduleSummary } from "@/lib/types";
import EnabledToggle from "./EnabledToggle";
import { classifyError } from "./errorClassify";
import { humanTrigger } from "./trigger";
import { KNOWN_RUN_STATUSES, formatDelta, latestRunBySchedule, toMs, type RunRow } from "./data";

export type SortDir = "asc" | "desc";

export function sortByNextFire(schedules: ScheduleSummary[], dir: SortDir): ScheduleSummary[] {
  const mul = dir === "asc" ? 1 : -1;
  return [...schedules].sort((a, b) => {
    if (a.next_fire_at == null && b.next_fire_at == null) return a.name.localeCompare(b.name);
    if (a.next_fire_at == null) return 1;
    if (b.next_fire_at == null) return -1;
    return (a.next_fire_at - b.next_fire_at) * mul;
  });
}

function NextFireCell({ schedule, nowMs }: { schedule: ScheduleSummary; nowMs: number }) {
  const t = useTranslations("schedules");
  const locale = useLocale();

  if (schedule.trigger_type === "github_poll") {
    return <span className="text-meta text-content-secondary">{t("card.watching")}</span>;
  }
  if (schedule.next_fire_at == null) {
    return <span className="text-meta text-content-muted">{t("table.notScheduled")}</span>;
  }

  const fireMs = toMs(schedule.next_fire_at);
  const delta = fireMs - nowMs;
  const overdue = delta < 0;
  const soon = !overdue && delta < 3_600_000;
  const absolute = new Date(fireMs).toLocaleString(locale, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  return (
    <div className="flex flex-col gap-0.5">
      <span className="font-data text-body text-content-primary">{absolute}</span>
      <span
        className="text-meta"
        style={{
          color: overdue
            ? "var(--status-warning)"
            : soon
              ? "var(--accent)"
              : "var(--content-secondary)",
        }}
      >
        {overdue
          ? t("card.overdue", { delta: formatDelta(-delta) })
          : t("card.in", { delta: formatDelta(delta) })}
      </span>
    </div>
  );
}

function LastRunCell({ run, nowMs }: { run: RunRow | undefined; nowMs: number }) {
  const t = useTranslations("schedules");
  const tError = useTranslations("schedules.error");
  const tStatus = useTranslations("history.status");

  if (!run) return <span className="text-meta text-content-muted">{t("table.neverRun")}</span>;

  const label = KNOWN_RUN_STATUSES.has(run.status)
    ? tStatus(run.status as Parameters<typeof tStatus>[0])
    : undefined;
  const errorLine = run.status === "failed" ? classifyError(run.error_detail, tError) : null;

  return (
    <div className="flex flex-col gap-0.5">
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

function ScheduleRow({
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
    <tr
      onClick={() => onOpen(schedule.id)}
      className={[
        "cursor-pointer border-b border-edge transition-colors duration-100 hover:bg-surface-overlay/60",
        schedule.enabled ? "" : "opacity-60",
      ].join(" ")}
    >
      <td className="px-3 py-2.5">
        <span
          className="block max-w-[280px] truncate font-data text-body font-semibold text-content-primary"
          title={schedule.description ?? schedule.name}
        >
          {schedule.name}
        </span>
      </td>
      <td className="px-3 py-2.5">
        <span className="font-data text-meta text-content-secondary" title={trigger.title}>
          {trigger.text}
        </span>
      </td>
      {/* stopPropagation so the toggle doesn't also open the row */}
      <td
        className="px-3 py-2.5"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
      >
        <EnabledToggle
          scheduleId={schedule.id}
          enabled={Boolean(schedule.enabled)}
          onToggled={onChanged}
        />
      </td>
      <td className="px-3 py-2.5">
        <LastRunCell run={lastRun} nowMs={nowMs} />
      </td>
      <td className="px-3 py-2.5">
        <NextFireCell schedule={schedule} nowMs={nowMs} />
      </td>
      <td className="px-3 py-2.5">
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
      </td>
    </tr>
  );
}

function SortableHeader({
  label,
  dir,
  onClick,
}: {
  label: string;
  dir: SortDir | null;
  onClick: () => void;
}) {
  return (
    <th
      className="px-3 py-2 font-medium"
      aria-sort={dir === "asc" ? "ascending" : dir === "desc" ? "descending" : "none"}
    >
      <button
        type="button"
        onClick={onClick}
        className="flex items-center gap-1 text-content-muted transition-colors duration-100 hover:text-content-primary"
      >
        {label}
        {dir === "asc" && <IconChevronUp size={11} strokeWidth={2.5} />}
        {dir === "desc" && <IconChevronDown size={11} strokeWidth={2.5} />}
      </button>
    </th>
  );
}

export default function SchedulesTable({
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
  const t = useTranslations("schedules");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const lastRuns = useMemo(() => latestRunBySchedule(runs), [runs]);
  const sorted = useMemo(() => sortByNextFire(schedules, sortDir), [schedules, sortDir]);

  return (
    <div className="min-h-0 flex-1 overflow-auto px-6 pb-6">
      <table className="w-full text-left" style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr
            className="border-b border-edge bg-surface-raised text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted"
            style={{ position: "sticky", top: 0, zIndex: 1 }}
          >
            <th className="px-3 py-2 font-medium">{t("table.colName")}</th>
            <th className="px-3 py-2 font-medium">{t("table.colTrigger")}</th>
            <th className="px-3 py-2 font-medium">{t("table.colEnabled")}</th>
            <th className="px-3 py-2 font-medium">{t("table.colLastRun")}</th>
            <SortableHeader
              label={t("table.colNextFire")}
              dir={sortDir}
              onClick={() => setSortDir((d) => (d === "asc" ? "desc" : "asc"))}
            />
            <th className="px-3 py-2 font-medium">{t("table.colActions")}</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((s) => (
            <ScheduleRow
              key={s.id}
              schedule={s}
              lastRun={lastRuns.get(s.id)}
              nowMs={nowMs}
              onChanged={onChanged}
              onOpen={onOpen}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
