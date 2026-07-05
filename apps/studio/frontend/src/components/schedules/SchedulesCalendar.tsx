/**
 * SchedulesCalendar — month grid, week grid, and day grid over real data.
 *
 * Past cells show actual runs (status-colored dots). Future cells show:
 *   - interval schedules: projected fires within the visible range
 *     (next_fire_at + N * interval_sec, deduped per visible cell)
 *   - cron schedules: single next_fire_at occurrence (no frontend cron parser)
 *   - github_poll schedules: a "polling" indicator on today only
 *     (poll triggers have no discrete per-day fire time)
 *
 * Month cells bucket by day; week/day views bucket by hour on a scrollable
 * hour grid. Clicking a day (or an hour cell) opens a detail strip below.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useLocale, useTranslations } from "use-intl";
import IconButton from "@/components/ui/IconButton";
import StatusPill from "@/components/ui/StatusPill";
import type { ScheduleSummary } from "@/lib/types";
import { KNOWN_RUN_STATUSES } from "./cards";
import { toMs, type RunRow } from "./data";

const STATUS_DOT: Record<string, string> = {
  running: "var(--status-running)",
  completed: "var(--status-success)",
  failed: "var(--status-error)",
  skipped: "var(--content-muted)",
  cancelled: "var(--content-muted)",
};

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
const HOUR_ROW_PX = 44;
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

  // Hour-granularity buckets — week/day grids only.
  const byHour = useMemo(
    () =>
      mode === "month"
        ? null
        : bucketItems(schedules, runs, range.startMs, range.endMs, today, hourKey),
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

  const timeOf = (ms: number) =>
    new Date(ms).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });

  const renderChip = (item: DayItem, key: number) =>
    item.kind === "poll" ? (
      <span
        key={key}
        className="flex min-w-0 items-center gap-1 text-meta leading-tight"
        title={`${item.schedule.name} · polling`}
      >
        <span
          aria-hidden="true"
          className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full"
          style={{ background: "var(--status-running)" }}
        />
        <span className="truncate text-content-secondary">{item.schedule.name}</span>
      </span>
    ) : (
      <span
        key={key}
        className="flex min-w-0 items-center gap-1 text-meta leading-tight"
        title={
          item.kind === "run"
            ? `${item.run.scheduleName} · ${item.run.status}`
            : `${item.schedule.name} · ${t("next")}`
        }
      >
        {item.kind === "run" ? (
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: STATUS_DOT[item.run.status] ?? "var(--content-muted)" }}
          />
        ) : (
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full border"
            style={{ borderColor: "var(--accent)" }}
          />
        )}
        <span className="shrink-0 font-data tabular-nums text-content-muted">
          {timeOf(item.atMs)}
        </span>
        <span className="truncate text-content-secondary">
          {item.kind === "run" ? item.run.scheduleName : item.schedule.name}
        </span>
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
              const items = byDay.get(key) ?? [];
              const isToday = key === todayKey;
              const isSelected = key === selectedDay;
              const overflow = items.length - MAX_VISIBLE_PER_DAY;
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
                    {items.slice(0, MAX_VISIBLE_PER_DAY).map((item, j) => renderChip(item, j))}
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
      ) : (
        <div className="overflow-hidden rounded-lg border border-edge">
          {/* Column header: gutter spacer + day headers. */}
          <div
            className="grid border-b border-edge"
            style={{ gridTemplateColumns: `48px repeat(${hourColumns.length}, minmax(0, 1fr))` }}
          >
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
                    selectedDay === key ? "bg-surface-overlay" : "hover:bg-surface-overlay/60",
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
            <div
              className="grid border-b border-edge"
              style={{ gridTemplateColumns: `48px repeat(${hourColumns.length}, minmax(0, 1fr))` }}
            >
              <div className="px-1.5 py-1 text-right font-data text-meta text-content-muted">
                {t("allDay")}
              </div>
              {hourColumns.map((d) => {
                const polls = (byDay.get(dayKey(d)) ?? []).filter((item) => item.kind === "poll");
                return (
                  <div
                    key={dayKey(d)}
                    className="flex flex-col gap-0.5 border-l border-edge px-1.5 py-1"
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
              <div
                key={hour}
                className="grid"
                style={{
                  gridTemplateColumns: `48px repeat(${hourColumns.length}, minmax(0, 1fr))`,
                }}
              >
                <div
                  className="border-b border-edge px-1.5 pt-0.5 text-right font-data text-meta tabular-nums text-content-muted"
                  style={{ minHeight: HOUR_ROW_PX }}
                >
                  {String(hour).padStart(2, "0")}:00
                </div>
                {hourColumns.map((d) => {
                  const dk = dayKey(d);
                  const items = byHour?.get(`${dk}H${hour}`) ?? [];
                  const overflow = items.length - MAX_VISIBLE_PER_HOUR;
                  return (
                    <button
                      key={dk}
                      type="button"
                      onClick={() => setSelectedDay(selectedDay === dk ? null : dk)}
                      className={[
                        "flex flex-col items-stretch gap-0.5 overflow-hidden border-b border-l border-edge p-1 text-left transition-colors duration-100",
                        selectedDay === dk
                          ? "bg-surface-overlay/40"
                          : "hover:bg-surface-overlay/60",
                      ].join(" ")}
                      style={{ minHeight: HOUR_ROW_PX }}
                    >
                      {items.slice(0, MAX_VISIBLE_PER_HOUR).map((item, j) => renderChip(item, j))}
                      {overflow > 0 && (
                        <span className="text-meta text-content-muted">
                          {t("more", { count: overflow })}
                        </span>
                      )}
                    </button>
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
