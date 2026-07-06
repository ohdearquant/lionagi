/**
 * Human-readable trigger phrases for the schedules table — translates cron/
 * interval/github_poll fields into a short sentence ("daily 18:00 UTC",
 * "every 45m", "on PR merge") instead of showing raw scheduler syntax.
 * Cron fields resolve in UTC (the scheduler engine runs on UTC epochs), so
 * simple daily/weekly/hourly patterns are rendered with a UTC suffix.
 */
import type { ScheduleSummary } from "@/lib/types";
import { formatInterval } from "./data";

type Translate = (key: string, values?: Record<string, string | number | Date>) => string;

const pad2 = (n: number) => String(n).padStart(2, "0");

interface CronFields {
  minute: string;
  hour: string;
  dom: string;
  month: string;
  dow: string;
}

function parseCron(expr: string): CronFields | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, dom, month, dow] = parts;
  return { minute, hour, dom, month, dow };
}

const isWild = (f: string) => f === "*";
const isNum = (f: string) => /^\d+$/.test(f);

/** Weekday name for a cron dow field (0=Sunday..6=Saturday, 7 treated as 0). */
function weekdayName(dow: number, locale: string): string {
  const normalized = dow % 7;
  // 2023-01-01 was a Sunday (UTC) — offset from it to land on the target weekday.
  const ref = new Date(Date.UTC(2023, 0, 1 + normalized));
  return ref.toLocaleDateString(locale, { weekday: "long", timeZone: "UTC" });
}

const GITHUB_EVENT_KEY: Record<string, string> = {
  pr_merged: "eventPrMerged",
  pr_opened: "eventPrOpened",
  pr_updated: "eventPrUpdated",
  pr_closed: "eventPrClosed",
};

function humanCron(expr: string, t: Translate, locale: string): string {
  const fields = parseCron(expr);
  if (!fields) return expr;
  const { minute, hour, dom, month, dow } = fields;

  if (isWild(minute) && isWild(hour) && isWild(dom) && isWild(month) && isWild(dow)) {
    return t("trigger.everyMinute");
  }

  const everyN = /^\*\/(\d+)$/.exec(minute);
  if (everyN && isWild(hour) && isWild(dom) && isWild(month) && isWild(dow)) {
    return t("trigger.everyMinutes", { n: Number(everyN[1]) });
  }

  if (isNum(minute) && isWild(hour) && isWild(dom) && isWild(month) && isWild(dow)) {
    return t("trigger.hourlyAt", { minute: pad2(Number(minute)) });
  }

  if (isNum(minute) && isNum(hour) && isWild(dom) && isWild(month)) {
    const time = `${pad2(Number(hour))}:${pad2(Number(minute))}`;
    if (isWild(dow)) return t("trigger.daily", { time });
    if (isNum(dow)) return t("trigger.weekly", { day: weekdayName(Number(dow), locale), time });
  }

  return expr;
}

/** Trigger column text + a precise tooltip (raw cron/interval/repo detail). */
export function humanTrigger(
  s: ScheduleSummary,
  t: Translate,
  locale: string,
): { text: string; title: string } {
  if (s.trigger_type === "cron" && s.cron_expr) {
    return { text: humanCron(s.cron_expr, t, locale), title: s.cron_expr };
  }
  if (s.trigger_type === "interval" && s.interval_sec != null) {
    const text = t("card.every", { interval: formatInterval(s.interval_sec) });
    return { text, title: text };
  }
  if (s.trigger_type === "github_poll" && s.github_repo) {
    const eventKey = GITHUB_EVENT_KEY[s.github_filter?.event ?? ""];
    const eventLabel = eventKey ? t(`trigger.${eventKey}`) : s.github_repo;
    const text = t("trigger.onEvent", { event: eventLabel });
    const poll =
      s.poll_interval_sec != null
        ? ` · ${t("card.every", { interval: formatInterval(s.poll_interval_sec) })}`
        : "";
    return { text, title: `${s.github_repo}${poll}` };
  }
  return { text: s.trigger_type, title: s.trigger_type };
}
