/**
 * SchedulesCalendar — month grid, agenda week, and day grid over real data.
 *
 * Past cells show actual runs (status-colored dots). Future cells show:
 *   - interval schedules: projected fires within the visible range
 *     (next_fire_at + N * interval_sec, deduped per visible cell)
 *   - cron schedules: single next_fire_at occurrence (no frontend cron parser)
 *   - github_poll schedules: a "polling" indicator on today only
 *     (poll triggers have no discrete per-day fire time)
 *
 * Month cells bucket by day. The week view is an agenda: seven day columns,
 * each listing its firings chronologically as full-width chips — no hour
 * grid, since firings are few and bursty rather than evenly spread across a
 * work day. The day view keeps the scrollable hour grid. Clicking a day (or
 * an hour cell, or a week agenda column) opens a detail strip below.
 * Repeated fires of the same schedule inside one cell collapse into a single
 * chip with a count badge — the detail strip below still lists every
 * individual fire, so "expand" is just a click away.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useLocale, useTranslations } from "use-intl";
import IconButton from "@/components/ui/IconButton";
import StatusPill from "@/components/ui/StatusPill";
import type { ScheduleSummary } from "@/lib/types";
import { KNOWN_RUN_STATUSES, toMs, type RunRow } from "./data";

const STATUS_DOT: Record<string, string> = {
  running: "var(--status-running)",
  completed: "var(--status-success)",
  failed: "var(--status-error)",
  skipped: "var(--content-muted)",
  cancelled: "var(--content-muted)",
};

/** Uniform chip shell — fixed height, truncating, consistent spacing —
 * shared by month cells, hour cells, and the all-day row. */
const CHIP_ROW =
  "flex h-5 min-w-0 shrink-0 items-center gap-1 rounded bg-surface-overlay/50 px-1 text-meta leading-none";

/** Full-width agenda chip shell — one line, same type scale as CHIP_ROW but
 * sized for a week agenda column instead of a cramped grid cell. */
const AGENDA_CHIP_ROW =
  "flex w-full min-w-0 items-center gap-1.5 rounded border border-edge/60 bg-surface-overlay/50 px-1.5 py-1 text-meta leading-none";

interface DayRun {
  kind: "run";
  atMs: number;
  run: RunRow;
}

interface DayFire {
  kind: "fire";
  atMs: number;
  schedule: ScheduleSummary;
}

interface DayPoll {
  kind: "poll";
  schedule: ScheduleSummary;
}

type DayItem = DayRun | DayFire | DayPoll;

type CalMode = "month" | "week" | "day";

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function hourKey(d: Date): string {
  return `${dayKey(d)}H${d.getHours()}`;
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** Monday-first start of the week containing d. */
function startOfWeek(d: Date): Date {
  const s = startOfDay(d);
  s.setDate(s.getDate() - ((s.getDay() + 6) % 7));
  return s;
}

const MAX_VISIBLE_PER_DAY = 3;
const MAX_VISIBLE_PER_HOUR = 3;
const HOUR_ROW_PX = 56;
/** Floor width for a day column in the week/day grids — narrower than this
 * and chips stop being legible, so the grid scrolls horizontally instead. */
const MIN_DAY_COL_PX = 128;
/** Initial scroll target for hour grids — work usually starts around 07:00. */
const SCROLL_TO_HOUR = 7;

/**
 * Bucket runs, projected fires, and poll indicators into cells keyed by
 * keyFn. Interval fires are deduped per (schedule, cell) so short intervals
 * never flood a cell.
 */
function bucketItems(
  schedules: ScheduleSummary[],
  runs: RunRow[],
  rangeStartMs: number,
  rangeEndMs: number,
  today: Date,
  keyFn: (d: Date) => string,
): Map<string, DayItem[]> {
  const map = new Map<string, DayItem[]>();
  const push = (key: string, item: DayItem) => {
    const list = map.get(key);
    if (list) list.push(item);
    else map.set(key, [item]);
  };

  for (const run of runs) {
    const atMs = toMs(run.fired_at);
    push(keyFn(new Date(atMs)), { kind: "run", atMs, run });
  }

  for (const s of schedules) {
    if (!s.enabled || s.next_fire_at == null) continue;
    const nextMs = toMs(s.next_fire_at);

    if (s.trigger_type === "interval" && s.interval_sec != null && s.interval_sec > 0) {
      const stepMs = s.interval_sec * 1000;
      let fireMs = nextMs;
      if (fireMs < rangeStartMs) {
        const stepsNeeded = Math.ceil((rangeStartMs - fireMs) / stepMs);
        fireMs += stepsNeeded * stepMs;
      }
      const seenCells = new Set<string>();
      while (fireMs <= rangeEndMs) {
        const key = keyFn(new Date(fireMs));
        if (!seenCells.has(key)) {
          seenCells.add(key);
          push(key, { kind: "fire", atMs: fireMs, schedule: s });
        }
        fireMs += stepMs;
      }
    } else if (s.trigger_type === "github_poll") {
      // No discrete fire time — indicator on today only, keyed by day so the
      // hour grids can hoist it into their all-day row.
      push(dayKey(today), { kind: "poll", schedule: s });
    } else {
      // cron (or any future trigger type without frontend parser): single
      // next_fire_at occurrence only.
      if (nextMs >= rangeStartMs && nextMs <= rangeEndMs) {
        push(keyFn(new Date(nextMs)), { kind: "fire", atMs: nextMs, schedule: s });
      }
    }
  }

  for (const list of map.values()) {
    list.sort((a, b) => {
      const aMs = a.kind === "poll" ? Infinity : a.atMs;
      const bMs = b.kind === "poll" ? Infinity : b.atMs;
      return aMs - bMs;
    });
  }
  return map;
}

/** A same-schedule cluster of runs/fires collapsed into one chip. Polls pass
 * through ungrouped (there is only ever one per day). */
interface ChipGroup {
  kind: "group";
  sourceKind: "run" | "fire";
  atMs: number;
  scheduleName: string;
  status?: string;
  count: number;
}

type ChipEntry = DayPoll | ChipGroup;

/**
 * Collapse repeated fires/runs of the same schedule within one cell into a
 * single ChipGroup carrying a count — a frequent schedule reads as one chip
 * with a "×N" badge instead of a wall of near-identical rows. Order is by
 * first occurrence; a group's displayed time/status track its latest item.
 */
function groupBySchedule(items: DayItem[]): ChipEntry[] {
  const order: ChipEntry[] = [];
  const groups = new Map<string, ChipGroup>();
  for (const item of items) {
    if (item.kind === "poll") {
      order.push(item);
      continue;
    }
    const scheduleId = item.kind === "run" ? item.run.schedule_id : item.schedule.id;
    const scheduleName = item.kind === "run" ? item.run.scheduleName : item.schedule.name;
    const groupKey = `${item.kind}:${scheduleId}`;
    const existing = groups.get(groupKey);
    if (existing) {
      existing.count += 1;
      if (item.atMs >= existing.atMs) {
        existing.atMs = item.atMs;
        if (item.kind === "run") existing.status = item.run.status;
      }
      continue;
    }
    const group: ChipGroup = {
      kind: "group",
      sourceKind: item.kind,
      atMs: item.atMs,
      scheduleName,
      status: item.kind === "run" ? item.run.status : undefined,
      count: 1,
    };
    groups.set(groupKey, group);
    order.push(group);
  }
  return order;
}

/** A run of consecutive same-schedule firings inside the same hour, shown
 * as one agenda chip carrying a time-range badge (e.g. "9 firings 09:00–09:50")
 * instead of a bare count. */
interface AgendaGroup {
  kind: "agendaGroup";
  sourceKind: "run" | "fire";
  startMs: number;
  endMs: number;
  scheduleName: string;
  status?: string;
  count: number;
}

type AgendaEntry = DayPoll | AgendaGroup;

/**
 * Build the agenda for one day column: chronological, with consecutive
 * same-schedule firings inside the same hour collapsed into one AgendaGroup.
 * Unlike groupBySchedule (whole-cell collapse for month/hour grids), a gap in
 * the hour or a different schedule always starts a fresh group — the badge's
 * time range stays a true, contiguous span.
 */
function groupAgendaByHour(items: DayItem[]): AgendaEntry[] {
  const out: AgendaEntry[] = [];
  let current: AgendaGroup | null = null;
  let currentScheduleId: string | null = null;
  for (const item of items) {
    if (item.kind === "poll") {
      out.push(item);
      current = null;
      continue;
    }
    const scheduleId = item.kind === "run" ? item.run.schedule_id : item.schedule.id;
    const scheduleName = item.kind === "run" ? item.run.scheduleName : item.schedule.name;
    const hour = new Date(item.atMs).getHours();
    if (
      current &&
      currentScheduleId === scheduleId &&
      current.sourceKind === item.kind &&
      new Date(current.endMs).getHours() === hour
    ) {
      current.count += 1;
      current.endMs = item.atMs;
      if (item.kind === "run") current.status = item.run.status;
      continue;
    }
    current = {
      kind: "agendaGroup",
      sourceKind: item.kind,
      startMs: item.atMs,
      endMs: item.atMs,
      scheduleName,
      status: item.kind === "run" ? item.run.status : undefined,
      count: 1,
    };
    currentScheduleId = scheduleId;
    out.push(current);
  }
  return out;
}

/** Truncate long schedule names in the middle so both the meaningful prefix
 * and suffix stay visible; the full name always remains in the title attr. */
function truncateMiddle(text: string, max = 30): string {
  if (text.length <= max) return text;
  const half = Math.floor((max - 1) / 2);
  return `${text.slice(0, half)}…${text.slice(text.length - half)}`;
}

export { groupAgendaByHour, truncateMiddle };
export type { AgendaEntry, AgendaGroup };

export default function SchedulesCalendar({
  schedules,
  runs,
}: {
  schedules: ScheduleSummary[];
  runs: RunRow[];
}) {
  const t = useTranslations("schedules.cal");
  const tStatus = useTranslations("history.status");
  const locale = useLocale();
  const statusLabel = (status: string): string | undefined =>
    KNOWN_RUN_STATUSES.has(status) ? tStatus(status as Parameters<typeof tStatus>[0]) : undefined;
  // Stable today reference — remounted at most once per component mount.
  const today = useMemo(() => new Date(), []);
  const [mode, setMode] = useState<CalMode>("month");
  const [anchor, setAnchor] = useState<Date>(() => startOfDay(today));
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const hourGridRef = useRef<HTMLDivElement | null>(null);

  // Visible range per mode.
  const range = useMemo(() => {
    if (mode === "month") {
      const start = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
      const end = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0, 23, 59, 59, 999);
      return { startMs: start.getTime(), endMs: end.getTime() };
    }
    if (mode === "week") {
      const start = startOfWeek(anchor);
      const end = new Date(start);
      end.setDate(end.getDate() + 7);
      return { startMs: start.getTime(), endMs: end.getTime() - 1 };
    }
    const start = startOfDay(anchor);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    return { startMs: start.getTime(), endMs: end.getTime() - 1 };
  }, [mode, anchor]);

  // Day-granularity buckets — month grid cells and the detail strip.
  const byDay = useMemo(
    () => bucketItems(schedules, runs, range.startMs, range.endMs, today, dayKey),
    [schedules, runs, range, today],
  );

  // Hour-granularity buckets — day grid only (the week view is an agenda
  // bucketed by day, reusing byDay below).
  const byHour = useMemo(
    () =>
      mode === "day"
        ? bucketItems(schedules, runs, range.startMs, range.endMs, today, hourKey)
        : null,
    [mode, schedules, runs, range, today],
  );

  // 42-cell Monday-first month grid.
  const monthCells = useMemo(() => {
    if (mode !== "month") return [];
    const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    const startOffset = (first.getDay() + 6) % 7;
    const out: { date: Date; inMonth: boolean }[] = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(anchor.getFullYear(), anchor.getMonth(), 1 - startOffset + i);
      out.push({ date: d, inMonth: d.getMonth() === anchor.getMonth() });
    }
    return out;
  }, [mode, anchor]);

  // Columns for the hour grids.
  const hourColumns = useMemo(() => {
    if (mode === "week") {
      const start = startOfWeek(anchor);
      return Array.from({ length: 7 }, (_, i) => {
        const d = new Date(start);
        d.setDate(d.getDate() + i);
        return d;
      });
    }
    if (mode === "day") return [startOfDay(anchor)];
    return [];
  }, [mode, anchor]);

  // Scroll hour grids to the working morning on mode/anchor change.
  useEffect(() => {
    if (mode !== "month" && hourGridRef.current) {
      hourGridRef.current.scrollTop = SCROLL_TO_HOUR * HOUR_ROW_PX;
    }
  }, [mode, anchor]);

  const todayKey = dayKey(today);

  const periodLabel = useMemo(() => {
    if (mode === "month") {
      return new Date(anchor.getFullYear(), anchor.getMonth(), 1).toLocaleDateString(locale, {
        month: "long",
        year: "numeric",
      });
    }
    if (mode === "week") {
      const start = startOfWeek(anchor);
      const end = new Date(start);
      end.setDate(end.getDate() + 6);
      const opts = { month: "short", day: "numeric" } as const;
      return `${start.toLocaleDateString(locale, opts)} – ${end.toLocaleDateString(locale, {
        ...opts,
        year: "numeric",
      })}`;
    }
    return anchor.toLocaleDateString(locale, {
      weekday: "long",
      month: "long",
      day: "numeric",
      year: "numeric",
    });
  }, [mode, anchor, locale]);

  const isCurrentPeriod = useMemo(() => {
    if (mode === "month")
      return anchor.getFullYear() === today.getFullYear() && anchor.getMonth() === today.getMonth();
    if (mode === "week") return startOfWeek(anchor).getTime() === startOfWeek(today).getTime();
    return dayKey(anchor) === todayKey;
  }, [mode, anchor, today, todayKey]);

  const weekdayLabels = useMemo(() => {
    // Monday-first; 2024-01-01 was a Monday.
    return Array.from({ length: 7 }, (_, i) =>
      new Date(2024, 0, 1 + i).toLocaleDateString(locale, { weekday: "short" }),
    );
  }, [locale]);

  function shiftPeriod(delta: number) {
    setAnchor((a) => {
      const d = new Date(a);
      if (mode === "month") d.setMonth(d.getMonth() + delta, 1);
      else if (mode === "week") d.setDate(d.getDate() + delta * 7);
      else d.setDate(d.getDate() + delta);
      return d;
    });
    setSelectedDay(null);
  }

  function switchMode(next: CalMode) {
    setMode(next);
    setSelectedDay(null);
  }

  const selectedItems = selectedDay ? (byDay.get(selectedDay) ?? []) : [];

  // Shared column template for the week/day header, all-day row, and every
  // hour row — one min-width floor so all three tracks line up and stay in
  // sync when the outer wrapper scrolls horizontally.
  const dayGridCols = `48px repeat(${hourColumns.length}, minmax(${MIN_DAY_COL_PX}px, 1fr))`;

  const timeOf = (ms: number) =>
    new Date(ms).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });

  const renderChip = (entry: ChipEntry, key: number) =>
    entry.kind === "poll" ? (
      <span key={key} className={CHIP_ROW} title={`${entry.schedule.name} · polling`}>
        <span
          aria-hidden="true"
          className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full"
          style={{ background: "var(--status-running)" }}
        />
        <span className="min-w-0 flex-1 truncate text-content-secondary">
          {entry.schedule.name}
        </span>
      </span>
    ) : (
      <span
        key={key}
        className={CHIP_ROW}
        title={
          entry.count > 1
            ? `${entry.scheduleName} × ${entry.count}`
            : entry.sourceKind === "run"
              ? `${entry.scheduleName} · ${entry.status}`
              : `${entry.scheduleName} · ${t("next")}`
        }
      >
        {entry.sourceKind === "run" ? (
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: STATUS_DOT[entry.status ?? ""] ?? "var(--content-muted)" }}
          />
        ) : (
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full border"
            style={{ borderColor: "var(--accent)" }}
          />
        )}
        {entry.count === 1 && (
          <span className="shrink-0 font-data tabular-nums text-content-muted">
            {timeOf(entry.atMs)}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-content-secondary">{entry.scheduleName}</span>
        {entry.count > 1 && (
          <span className="shrink-0 rounded-sm bg-[var(--accent)]/15 px-1 font-data text-[10px] font-semibold tabular-nums text-[var(--accent)]">
            {t("runCount", { count: entry.count })}
          </span>
        )}
      </span>
    );

  const renderAgendaChip = (entry: AgendaEntry, key: number) =>
    entry.kind === "poll" ? (
      <span key={key} className={AGENDA_CHIP_ROW} title={`${entry.schedule.name} · polling`}>
        <span
          aria-hidden="true"
          className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full"
          style={{ background: "var(--status-running)" }}
        />
        <span className="min-w-0 flex-1 truncate text-content-secondary">
          {truncateMiddle(entry.schedule.name)}
        </span>
      </span>
    ) : (
      <span
        key={key}
        className={AGENDA_CHIP_ROW}
        title={
          entry.count > 1
            ? `${entry.scheduleName} · ${t("rangeBadge", {
                count: entry.count,
                start: timeOf(entry.startMs),
                end: timeOf(entry.endMs),
              })}`
            : entry.sourceKind === "run"
              ? `${entry.scheduleName} · ${entry.status}`
              : `${entry.scheduleName} · ${t("next")}`
        }
      >
        {entry.sourceKind === "run" ? (
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: STATUS_DOT[entry.status ?? ""] ?? "var(--content-muted)" }}
          />
        ) : (
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full border"
            style={{ borderColor: "var(--accent)" }}
          />
        )}
        {entry.count === 1 && (
          <span className="shrink-0 font-data tabular-nums text-content-muted">
            {timeOf(entry.startMs)}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-content-secondary">
          {truncateMiddle(entry.scheduleName)}
        </span>
        {entry.count > 1 && (
          <span className="shrink-0 rounded-sm bg-[var(--accent)]/15 px-1 font-data text-[10px] font-semibold tabular-nums text-[var(--accent)]">
            {t("rangeBadge", {
              count: entry.count,
              start: timeOf(entry.startMs),
              end: timeOf(entry.endMs),
            })}
          </span>
        )}
      </span>
    );

  return (
    <div className="flex flex-col gap-3 px-6 pb-6">
      {/* Period nav + view switcher */}
      <div className="flex items-center gap-2">
        <IconButton
          aria-label={t("prevPeriod")}
          size="md"
          onClick={() => shiftPeriod(-1)}
          className="border border-edge"
        >
          ‹
        </IconButton>
        <IconButton
          aria-label={t("nextPeriod")}
          size="md"
          onClick={() => shiftPeriod(1)}
          className="border border-edge"
        >
          ›
        </IconButton>
        <span className="font-data text-label font-semibold text-content-primary">
          {periodLabel}
        </span>
        {!isCurrentPeriod && (
          <button
            type="button"
            onClick={() => {
              setAnchor(startOfDay(today));
              setSelectedDay(null);
            }}
            className="rounded border border-edge px-2 py-0.5 text-meta text-content-secondary transition-colors duration-100 hover:border-edge-strong hover:text-content-primary"
          >
            {t("today")}
          </button>
        )}
        <div className="flex-1" />
        <div className="flex overflow-hidden rounded border border-edge">
          {(["month", "week", "day"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => switchMode(m)}
              aria-pressed={mode === m}
              className={[
                "px-2.5 py-1 text-meta transition-colors duration-100",
                mode === m
                  ? "bg-surface-overlay font-semibold text-content-primary"
                  : "text-content-secondary hover:text-content-primary",
              ].join(" ")}
            >
              {m === "month" ? t("viewMonth") : m === "week" ? t("viewWeek") : t("viewDay")}
            </button>
          ))}
        </div>
      </div>

      {mode === "month" ? (
        <>
          {/* Weekday header */}
          <div className="grid grid-cols-7">
            {weekdayLabels.map((w) => (
              <div
                key={w}
                className="px-2 pb-1 font-data text-meta uppercase tracking-wider text-content-muted"
              >
                {w}
              </div>
            ))}
          </div>

          {/* Month grid — fixed 96px rows, overflow-hidden so items never blow out the row. */}
          <div className="grid grid-cols-7 overflow-hidden rounded-lg border border-edge">
            {monthCells.map(({ date, inMonth }, i) => {
              const key = dayKey(date);
              const entries = groupBySchedule(byDay.get(key) ?? []);
              const isToday = key === todayKey;
              const isSelected = key === selectedDay;
              const overflow = entries.length - MAX_VISIBLE_PER_DAY;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setSelectedDay(isSelected ? null : key)}
                  className={[
                    "flex h-[96px] flex-col items-stretch border-edge p-1.5 text-left transition-colors duration-100",
                    i % 7 !== 6 ? "border-r" : "",
                    i < 35 ? "border-b" : "",
                    inMonth ? "" : "opacity-40",
                    isSelected ? "bg-surface-overlay" : "hover:bg-surface-overlay/60",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  {/* Day number — top-left, today gets accent ring. */}
                  <span
                    className={[
                      "mb-0.5 self-start font-data text-meta tabular-nums",
                      isToday
                        ? "flex h-5 w-5 items-center justify-center rounded-full bg-[var(--accent)] font-semibold text-black"
                        : "text-content-muted",
                    ].join(" ")}
                  >
                    {date.getDate()}
                  </span>
                  {/* Items area — overflow hidden to hold fixed row height. */}
                  <div className="flex flex-1 flex-col gap-0.5 overflow-hidden">
                    {entries.slice(0, MAX_VISIBLE_PER_DAY).map((entry, j) => renderChip(entry, j))}
                    {overflow > 0 && (
                      <span className="text-meta text-content-muted">
                        {t("more", { count: overflow })}
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </>
      ) : mode === "week" ? (
        // Agenda week: 7 equal day columns, each a chronological chip list —
        // no hour gutter, no all-day row. Firings are few and bursty, not
        // evenly spread across a work day, so a time grid is mostly empty.
        <div className="overflow-hidden rounded-lg border border-edge">
          {/* Column header — day-of-week + date, today gets the accent circle. */}
          <div className="grid grid-cols-7 border-b border-edge">
            {hourColumns.map((d) => {
              const key = dayKey(d);
              const isToday = key === todayKey;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setSelectedDay(selectedDay === key ? null : key)}
                  className={[
                    "flex items-center gap-1.5 border-l border-edge px-2 py-1.5 text-left transition-colors duration-100 first:border-l-0",
                    selectedDay === key
                      ? "bg-surface-overlay"
                      : isToday
                        ? "bg-[var(--today-tint)] hover:bg-surface-overlay/60"
                        : "hover:bg-surface-overlay/60",
                  ].join(" ")}
                >
                  <span className="font-data text-meta uppercase tracking-wider text-content-muted">
                    {d.toLocaleDateString(locale, { weekday: "short" })}
                  </span>
                  <span
                    className={[
                      "font-data text-meta tabular-nums",
                      isToday
                        ? "flex h-5 w-5 items-center justify-center rounded-full bg-[var(--accent)] font-semibold text-black"
                        : "text-content-secondary",
                    ].join(" ")}
                  >
                    {d.getDate()}
                  </span>
                </button>
              );
            })}
          </div>

          {/* Day columns — agenda chips, top-aligned, scroll inside the
              column rather than stretching every column to match the
              busiest day. */}
          <div className="grid grid-cols-7">
            {hourColumns.map((d) => {
              const dk = dayKey(d);
              const isToday = dk === todayKey;
              const dayItems = byDay.get(dk) ?? [];
              const polls = dayItems.filter((item): item is DayPoll => item.kind === "poll");
              const timed = dayItems.filter((item) => item.kind !== "poll");
              const entries: AgendaEntry[] = [...polls, ...groupAgendaByHour(timed)];
              const openHere = () => setSelectedDay(selectedDay === dk ? null : dk);
              return (
                <div
                  key={dk}
                  role="button"
                  tabIndex={0}
                  onClick={openHere}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      openHere();
                    }
                  }}
                  className={[
                    "flex max-h-[420px] flex-col items-stretch gap-1 overflow-y-auto border-l border-edge p-1.5 text-left transition-colors duration-100 first:border-l-0 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--accent)]",
                    selectedDay === dk
                      ? "bg-surface-overlay/40"
                      : isToday
                        ? "bg-[var(--today-tint)] hover:bg-surface-overlay/60"
                        : "hover:bg-surface-overlay/60",
                  ].join(" ")}
                >
                  {entries.length === 0 ? (
                    <span className="pt-1 text-meta text-content-muted">{t("emptyDay")}</span>
                  ) : (
                    entries.map((entry, j) => renderAgendaChip(entry, j))
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        // Horizontal scroll lives HERE, on the one shared ancestor of the
        // header/all-day/hour-grid tracks — they scroll together in lockstep
        // and the page body never gains a sideways scrollbar.
        <div className="overflow-x-auto rounded-lg border border-edge">
          {/* Column header: gutter spacer + day headers. */}
          <div className="grid border-b border-edge" style={{ gridTemplateColumns: dayGridCols }}>
            <div />
            {hourColumns.map((d) => {
              const key = dayKey(d);
              const isToday = key === todayKey;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setSelectedDay(selectedDay === key ? null : key)}
                  className={[
                    "flex items-center gap-1.5 border-l border-edge px-2 py-1.5 text-left transition-colors duration-100",
                    selectedDay === key
                      ? "bg-surface-overlay"
                      : isToday
                        ? "bg-[var(--today-tint)] hover:bg-surface-overlay/60"
                        : "hover:bg-surface-overlay/60",
                  ].join(" ")}
                >
                  <span className="font-data text-meta uppercase tracking-wider text-content-muted">
                    {d.toLocaleDateString(locale, { weekday: "short" })}
                  </span>
                  <span
                    className={[
                      "font-data text-meta tabular-nums",
                      isToday
                        ? "flex h-5 w-5 items-center justify-center rounded-full bg-[var(--accent)] font-semibold text-black"
                        : "text-content-secondary",
                    ].join(" ")}
                  >
                    {d.getDate()}
                  </span>
                </button>
              );
            })}
          </div>

          {/* All-day row — poll indicators live here (they have no fire time). */}
          {hourColumns.some((d) =>
            (byDay.get(dayKey(d)) ?? []).some((item) => item.kind === "poll"),
          ) && (
            <div className="grid border-b border-edge" style={{ gridTemplateColumns: dayGridCols }}>
              <div className="px-1.5 py-1 text-right font-data text-meta text-content-muted">
                {t("allDay")}
              </div>
              {hourColumns.map((d) => {
                const dk = dayKey(d);
                const polls = (byDay.get(dk) ?? []).filter((item) => item.kind === "poll");
                return (
                  <div
                    key={dk}
                    className={[
                      "flex flex-col gap-0.5 border-l border-edge px-1.5 py-1",
                      dk === todayKey ? "bg-[var(--today-tint)]" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    {polls.map((item, j) => renderChip(item, j))}
                  </div>
                );
              })}
            </div>
          )}

          {/* Hour grid — scrollable, opens at the working morning. */}
          <div ref={hourGridRef} className="max-h-[560px] overflow-y-auto">
            {Array.from({ length: 24 }, (_, hour) => (
              <div key={hour} className="grid" style={{ gridTemplateColumns: dayGridCols }}>
                <div
                  className="border-b border-edge px-1.5 pt-0.5 text-right font-data text-meta tabular-nums text-content-muted"
                  style={{ minHeight: HOUR_ROW_PX }}
                >
                  {String(hour).padStart(2, "0")}:00
                </div>
                {hourColumns.map((d) => {
                  const dk = dayKey(d);
                  const isToday = dk === todayKey;
                  const entries = groupBySchedule(byHour?.get(`${dk}H${hour}`) ?? []);
                  const overflow = entries.length - MAX_VISIBLE_PER_HOUR;
                  const openHere = () => setSelectedDay(selectedDay === dk ? null : dk);
                  return (
                    <div
                      key={dk}
                      role="button"
                      tabIndex={0}
                      onClick={openHere}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          openHere();
                        }
                      }}
                      className={[
                        "flex cursor-pointer flex-col items-stretch gap-0.5 overflow-hidden border-b border-l border-edge p-1 text-left transition-colors duration-100 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--accent)]",
                        selectedDay === dk
                          ? "bg-surface-overlay/40"
                          : isToday
                            ? "bg-[var(--today-tint)] hover:bg-surface-overlay/60"
                            : "hover:bg-surface-overlay/60",
                      ].join(" ")}
                      style={{ minHeight: HOUR_ROW_PX }}
                    >
                      {entries
                        .slice(0, MAX_VISIBLE_PER_HOUR)
                        .map((entry, j) => renderChip(entry, j))}
                      {overflow > 0 && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            switchMode("day");
                            setAnchor(startOfDay(d));
                            setSelectedDay(dk);
                          }}
                          className="w-fit text-meta text-content-muted underline-offset-2 hover:text-content-primary hover:underline"
                        >
                          {t("more", { count: overflow })}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Day detail strip */}
      {selectedDay && (
        <div className="flex flex-col gap-1 rounded-lg border border-edge p-3">
          <span className="pb-1 font-data text-meta uppercase tracking-wider text-content-muted">
            {new Date(`${selectedDay}T00:00:00`).toLocaleDateString(locale, {
              weekday: "long",
              month: "long",
              day: "numeric",
            })}
          </span>
          {selectedItems.length === 0 ? (
            <p className="py-2 text-body text-content-muted">{t("noItems")}</p>
          ) : (
            selectedItems.map((item, i) =>
              item.kind === "poll" ? (
                <div key={i} className="flex items-center gap-3 py-1">
                  <span
                    aria-hidden="true"
                    className="h-2 w-2 shrink-0 animate-pulse rounded-full"
                    style={{ background: "var(--status-running)" }}
                  />
                  <span className="min-w-0 flex-1 truncate font-data text-body text-content-primary">
                    {item.schedule.name}
                  </span>
                  <span className="shrink-0 text-meta text-content-muted">
                    {item.schedule.github_repo ?? "polling"}
                  </span>
                </div>
              ) : item.kind === "run" ? (
                <div key={i} className="flex items-center gap-3 py-1">
                  <span className="w-12 shrink-0 font-data text-meta tabular-nums text-content-muted">
                    {timeOf(item.atMs)}
                  </span>
                  <StatusPill
                    value={item.run.status}
                    taxonomy="session"
                    label={statusLabel(item.run.status)}
                  />
                  <span className="min-w-0 flex-1 truncate font-data text-body text-content-primary">
                    {item.run.scheduleName}
                  </span>
                  {item.run.invocation_id && (
                    <Link
                      to="/fleet"
                      className="shrink-0 text-meta text-accent underline-offset-2 hover:underline"
                    >
                      {item.run.invocation_id.slice(0, 8)} →
                    </Link>
                  )}
                </div>
              ) : (
                <div key={i} className="flex items-center gap-3 py-1">
                  <span className="w-12 shrink-0 font-data text-meta tabular-nums text-content-muted">
                    {timeOf(item.atMs)}
                  </span>
                  <span
                    className="inline-flex items-center rounded border px-1.5 py-0.5 text-meta leading-none"
                    style={{ borderColor: "var(--accent)", color: "var(--accent)" }}
                  >
                    {t("next")}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-data text-body text-content-primary">
                    {item.schedule.name}
                  </span>
                </div>
              ),
            )
          )}
        </div>
      )}
    </div>
  );
}
