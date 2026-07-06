/**
 * humanTrigger — cron/interval/github_poll → human phrase. Uses a fake
 * translator so the tests check which key + values were resolved without
 * depending on the real message catalog (that's covered by i18n/locales.test.ts).
 */
import { describe, it, expect } from "vitest";
import { humanTrigger } from "./trigger";
import type { ScheduleSummary } from "@/lib/types";

function schedule(overrides: Partial<ScheduleSummary> = {}): ScheduleSummary {
  return {
    id: "sched-1",
    name: "nightly-build",
    description: null,
    enabled: 1,
    trigger_type: "cron",
    cron_expr: "0 * * * *",
    interval_sec: null,
    github_repo: null,
    poll_interval_sec: null,
    action_kind: "agent",
    action_model: null,
    action_prompt: null,
    action_agent: null,
    action_playbook: null,
    action_project: null,
    on_success: null,
    on_fail: null,
    last_fired_at: null,
    next_fire_at: null,
    missed_fire_policy: "skip",
    overlap_policy: "skip",
    project: null,
    created_at: 0,
    updated_at: 0,
    ...overrides,
  };
}

// Renders "key" or "key{a:1,b:2}" so assertions can check both the chosen
// phrase and the exact values passed to it.
function fakeT(key: string, values?: Record<string, string | number | Date>): string {
  if (!values) return key;
  const parts = Object.entries(values).map(([k, v]) => `${k}:${v}`);
  return `${key}{${parts.join(",")}}`;
}

const LOCALE = "en-US";

describe("humanTrigger — cron", () => {
  it("* * * * * -> every minute", () => {
    const { text } = humanTrigger(schedule({ cron_expr: "* * * * *" }), fakeT, LOCALE);
    expect(text).toBe("trigger.everyMinute");
  });

  it("*/15 * * * * -> every 15 minutes", () => {
    const { text } = humanTrigger(schedule({ cron_expr: "*/15 * * * *" }), fakeT, LOCALE);
    expect(text).toBe("trigger.everyMinutes{n:15}");
  });

  it("30 * * * * -> hourly at :30", () => {
    const { text } = humanTrigger(schedule({ cron_expr: "30 * * * *" }), fakeT, LOCALE);
    expect(text).toBe("trigger.hourlyAt{minute:30}");
  });

  it("0 18 * * * -> daily 18:00", () => {
    const { text, title } = humanTrigger(schedule({ cron_expr: "0 18 * * *" }), fakeT, LOCALE);
    expect(text).toBe("trigger.daily{time:18:00}");
    expect(title).toBe("0 18 * * *");
  });

  it("5 9 * * * -> daily 09:05 (single-digit fields zero-pad)", () => {
    const { text } = humanTrigger(schedule({ cron_expr: "5 9 * * *" }), fakeT, LOCALE);
    expect(text).toBe("trigger.daily{time:09:05}");
  });

  it("0 9 * * 1 -> weekly Monday 09:00", () => {
    const { text } = humanTrigger(schedule({ cron_expr: "0 9 * * 1" }), fakeT, LOCALE);
    expect(text).toBe("trigger.weekly{day:Monday,time:09:00}");
  });

  it("0 9 * * 0 -> weekly Sunday (dow 0)", () => {
    const { text } = humanTrigger(schedule({ cron_expr: "0 9 * * 0" }), fakeT, LOCALE);
    expect(text).toBe("trigger.weekly{day:Sunday,time:09:00}");
  });

  it("falls back to the raw cron expression for unrecognized patterns", () => {
    const { text, title } = humanTrigger(
      schedule({ cron_expr: "0,30 9-17 * * 1-5" }),
      fakeT,
      LOCALE,
    );
    expect(text).toBe("0,30 9-17 * * 1-5");
    expect(title).toBe("0,30 9-17 * * 1-5");
  });

  it("falls back to the raw expression when field count is not 5", () => {
    const { text } = humanTrigger(schedule({ cron_expr: "0 18 * * * *" }), fakeT, LOCALE);
    expect(text).toBe("0 18 * * * *");
  });
});

describe("humanTrigger — interval", () => {
  it("renders card.every with a formatted interval, and uses it as the title too", () => {
    const { text, title } = humanTrigger(
      schedule({ trigger_type: "interval", cron_expr: null, interval_sec: 2700 }),
      fakeT,
      LOCALE,
    );
    expect(text).toBe("card.every{interval:45m}");
    expect(title).toBe(text);
  });
});

describe("humanTrigger — github_poll", () => {
  it("pr_merged -> on PR merge, title carries the repo + poll interval", () => {
    const { text, title } = humanTrigger(
      schedule({
        trigger_type: "github_poll",
        cron_expr: null,
        github_repo: "lion/lionagi",
        poll_interval_sec: 300,
        github_filter: { event: "pr_merged" },
      }),
      fakeT,
      LOCALE,
    );
    expect(text).toBe("trigger.onEvent{event:trigger.eventPrMerged}");
    expect(title).toBe("lion/lionagi · card.every{interval:5m}");
  });

  it("pr_opened / pr_updated / pr_closed map to their own event labels", () => {
    const cases: Array<[string, string]> = [
      ["pr_opened", "trigger.eventPrOpened"],
      ["pr_updated", "trigger.eventPrUpdated"],
      ["pr_closed", "trigger.eventPrClosed"],
    ];
    for (const [event, expectedKey] of cases) {
      const { text } = humanTrigger(
        schedule({
          trigger_type: "github_poll",
          cron_expr: null,
          github_repo: "lion/lionagi",
          github_filter: { event },
        }),
        fakeT,
        LOCALE,
      );
      expect(text).toBe(`trigger.onEvent{event:${expectedKey}}`);
    }
  });

  it("falls back to the raw repo name when no event filter is set", () => {
    const { text } = humanTrigger(
      schedule({ trigger_type: "github_poll", cron_expr: null, github_repo: "lion/lionagi" }),
      fakeT,
      LOCALE,
    );
    expect(text).toBe("trigger.onEvent{event:lion/lionagi}");
  });

  it("title omits the poll interval segment when poll_interval_sec is absent", () => {
    const { title } = humanTrigger(
      schedule({
        trigger_type: "github_poll",
        cron_expr: null,
        github_repo: "lion/lionagi",
        poll_interval_sec: null,
        github_filter: { event: "pr_merged" },
      }),
      fakeT,
      LOCALE,
    );
    expect(title).toBe("lion/lionagi");
  });
});
