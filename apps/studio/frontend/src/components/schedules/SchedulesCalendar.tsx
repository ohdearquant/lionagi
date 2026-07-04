/**
 * SchedulesCalendar — month grid over real data.
 *
 * Past cells show actual runs (status-colored dots). Future cells show:
 *   - interval schedules: projected fires within the visible month
 *     (next_fire_at + N * interval_sec, while still in-month)
 *   - cron schedules: single next_fire_at occurrence (no frontend cron parser)
 *   - github_poll schedules: a "polling" indicator on today's cell only
 *     (poll triggers have no discrete per-day fire time)
 *
 * Clicking a day opens a detail strip below the grid.
 */
import { useMemo, useState } from "react";
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

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const MAX_VISIBLE_PER_DAY = 3;

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
  const [view, setView] = useState<{ year: number; month: number }>({
    year: today.getFullYear(),
    month: today.getMonth(),
  });
  const [selectedDay, setSelectedDay] = useState<string | null>(null);

  // Bucket runs + next fires by local day.
  const byDay = useMemo(() => {
    const map = new Map<string, DayItem[]>();
    const push = (key: string, item: DayItem) => {
      const list = map.get(key);
      if (list) list.push(item);
      else map.set(key, [item]);
    };

    // Past runs — bucketed by fired_at date.
    for (const run of runs) {
      const atMs = toMs(run.fired_at);
      push(dayKey(new Date(atMs)), { kind: "run", atMs, run });
    }

    // Month window for future projections.
    const monthStart = new Date(view.year, view.month, 1).getTime();
    const monthEnd = new Date(view.year, view.month + 1, 0, 23, 59, 59, 999).getTime();

    for (const s of schedules) {
      if (!s.enabled || s.next_fire_at == null) continue;
      const nextMs = toMs(s.next_fire_at);

      if (s.trigger_type === "interval" && s.interval_sec != null && s.interval_sec > 0) {
        // Project all interval fires that land inside the visible month.
        const stepMs = s.interval_sec * 1000;
        let fireMs = nextMs;
        // Advance forward from next_fire_at until we enter or pass the month.
        if (fireMs < monthStart) {
          const stepsNeeded = Math.ceil((monthStart - fireMs) / stepMs);
          fireMs += stepsNeeded * stepMs;
        }
        // Emit at most one fire per (schedule, day) to avoid visual clutter on
        // very short intervals — the chip already shows the time.
        const seenDays = new Set<string>();
        while (fireMs <= monthEnd) {
          const dk = dayKey(new Date(fireMs));
          if (!seenDays.has(dk)) {
            seenDays.add(dk);
            push(dk, { kind: "fire", atMs: fireMs, schedule: s });
          }
          fireMs += stepMs;
        }
      } else if (s.trigger_type === "github_poll") {
        // github_poll has no discrete per-day fire time — show a polling
        // indicator on today's cell only.
        push(dayKey(today), { kind: "poll", schedule: s });
      } else {
        // cron (or any future trigger type without frontend parser): single
        // next_fire_at occurrence only.
        if (nextMs >= monthStart && nextMs <= monthEnd) {
          push(dayKey(new Date(nextMs)), { kind: "fire", atMs: nextMs, schedule: s });
        }
      }
    }

    for (const list of map.values()) {
      list.sort((a, b) => {
        // Polls have no timestamp — sort them last.
        const aMs = a.kind === "poll" ? Infinity : a.atMs;
        const bMs = b.kind === "poll" ? Infinity : b.atMs;
        return aMs - bMs;
      });
    }
    return map;
  }, [runs, schedules, view, today]);

  // 42-cell Monday-first grid.
  const cells = useMemo(() => {
    const first = new Date(view.year, view.month, 1);
    const startOffset = (first.getDay() + 6) % 7;
    const out: { date: Date; inMonth: boolean }[] = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(view.year, view.month, 1 - startOffset + i);
      out.push({ date: d, inMonth: d.getMonth() === view.month });
    }
    return out;
  }, [view]);

  const monthLabel = new Date(view.year, view.month, 1).toLocaleDateString(locale, {
    month: "long",
    year: "numeric",
  });
  const todayKey = dayKey(today);

  const weekdayLabels = useMemo(() => {
    // Monday-first; 2024-01-01 was a Monday.
    return Array.from({ length: 7 }, (_, i) =>
      new Date(2024, 0, 1 + i).toLocaleDateString(locale, { weekday: "short" }),
    );
  }, [locale]);

  function shiftMonth(delta: number) {
    setView((v) => {
      const d = new Date(v.year, v.month + delta, 1);
      return { year: d.getFullYear(), month: d.getMonth() };
    });
    setSelectedDay(null);
  }

  const selectedItems = selectedDay ? (byDay.get(selectedDay) ?? []) : [];

  const timeOf = (ms: number) =>
    new Date(ms).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });

  return (
    <div className="flex flex-col gap-3 px-6 pb-6">
      {/* Month nav */}
      <div className="flex items-center gap-2">
        <IconButton
          aria-label={t("prevMonth")}
          size="md"
          onClick={() => shiftMonth(-1)}
          className="border border-edge"
        >
          ‹
        </IconButton>
        <IconButton
          aria-label={t("nextMonth")}
          size="md"
          onClick={() => shiftMonth(1)}
          className="border border-edge"
        >
          ›
        </IconButton>
        <span className="font-data text-label font-semibold text-content-primary">
          {monthLabel}
        </span>
        {(view.year !== today.getFullYear() || view.month !== today.getMonth()) && (
          <button
            type="button"
            onClick={() => {
              setView({ year: today.getFullYear(), month: today.getMonth() });
              setSelectedDay(null);
            }}
            className="rounded border border-edge px-2 py-0.5 text-meta text-content-secondary transition-colors duration-100 hover:border-edge-strong hover:text-content-primary"
          >
            {t("today")}
          </button>
        )}
      </div>

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
        {cells.map(({ date, inMonth }, i) => {
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
                {items.slice(0, MAX_VISIBLE_PER_DAY).map((item, j) =>
                  item.kind === "poll" ? (
                    <span
                      key={j}
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
                      key={j}
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
                          style={{
                            background: STATUS_DOT[item.run.status] ?? "var(--content-muted)",
                          }}
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
                  ),
                )}
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
                      to="/history"
                      search={{ tab: "run" }}
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
