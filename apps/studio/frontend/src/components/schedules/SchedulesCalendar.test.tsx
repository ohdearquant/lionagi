/**
 * SchedulesCalendar week-agenda tests.
 *
 * Two layers, matching the existing project style (no @testing-library/react
 * here — see history/RunDetail.test.tsx):
 * - Pure logic: groupAgendaByHour (same-schedule, same-hour, consecutive
 *   collapse into one range-badged chip) and truncateMiddle.
 * - Source contract: the week branch renders 7 agenda columns with no hour
 *   gutter / all-day row, and uses the empty-day placeholder + range badge.
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { groupAgendaByHour, truncateMiddle } from "./SchedulesCalendar";
import type { ScheduleSummary } from "@/lib/types";
import type { RunRow } from "./data";

const CALENDAR_FILE = path.resolve(__dirname, "SchedulesCalendar.tsx");
const SRC = fs.readFileSync(CALENDAR_FILE, "utf-8");

function schedule(overrides: Partial<ScheduleSummary> = {}): ScheduleSummary {
  return {
    id: "sched-1",
    name: "nightly-build",
    description: null,
    enabled: 1,
    trigger_type: "cron",
    cron_expr: "*/10 * * * *",
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

function runRow(overrides: Partial<RunRow> = {}): RunRow {
  return {
    id: "run-1",
    schedule_id: "sched-1",
    scheduleName: "nightly-build",
    invocation_id: null,
    trigger_context: {},
    action_kind: "agent",
    status: "completed",
    exit_code: 0,
    chain_depth: 0,
    fired_at: 0,
    ended_at: null,
    error_detail: null,
    ...overrides,
  };
}

const MIN = 60_000;
// 2026-07-06 (Monday) 09:00 local, as an epoch-ms base for fixture firings.
const DAY_BASE = new Date(2026, 6, 6, 9, 0, 0, 0).getTime();

function fire(atMs: number, s: ScheduleSummary) {
  return { kind: "fire" as const, atMs, schedule: s };
}

function run(atMs: number, r: RunRow) {
  return { kind: "run" as const, atMs, run: r };
}

// ─── groupAgendaByHour ────────────────────────────────────────────────────

describe("groupAgendaByHour — same-schedule, same-hour consecutive collapse", () => {
  it("collapses consecutive same-schedule fires within the same hour into one range-badged group", () => {
    const s = schedule({ id: "s1", name: "poller" });
    const items = Array.from({ length: 9 }, (_, i) => fire(DAY_BASE + i * 5 * MIN, s));
    const entries = groupAgendaByHour(items);
    expect(entries).toHaveLength(1);
    const group = entries[0];
    if (group.kind !== "agendaGroup") throw new Error("expected agendaGroup");
    expect(group.count).toBe(9);
    expect(group.startMs).toBe(DAY_BASE);
    expect(group.endMs).toBe(DAY_BASE + 8 * 5 * MIN);
    expect(group.scheduleName).toBe("poller");
  });

  it("does not merge fires that cross an hour boundary", () => {
    const s = schedule({ id: "s1", name: "poller" });
    // 09:50 and 10:05 — same schedule, but different hours.
    const items = [fire(DAY_BASE + 50 * MIN, s), fire(DAY_BASE + 65 * MIN, s)];
    const entries = groupAgendaByHour(items);
    expect(entries).toHaveLength(2);
    expect(entries.every((e) => e.kind === "agendaGroup" && e.count === 1)).toBe(true);
  });

  it("does not merge different schedules even within the same hour", () => {
    const a = schedule({ id: "s1", name: "alpha" });
    const b = schedule({ id: "s2", name: "beta" });
    const items = [fire(DAY_BASE, a), fire(DAY_BASE + 5 * MIN, b), fire(DAY_BASE + 10 * MIN, a)];
    const entries = groupAgendaByHour(items);
    // alpha, beta, alpha — the second alpha is not adjacent to the first
    // (beta interrupts), so it starts its own group.
    expect(entries).toHaveLength(3);
    expect(entries.map((e) => (e.kind === "agendaGroup" ? e.scheduleName : ""))).toEqual([
      "alpha",
      "beta",
      "alpha",
    ]);
  });

  it("does not merge runs with fires even for the same schedule", () => {
    const s = schedule({ id: "s1", name: "mixed" });
    const r = runRow({ schedule_id: "s1", scheduleName: "mixed", fired_at: DAY_BASE });
    const items = [run(DAY_BASE, r), fire(DAY_BASE + 5 * MIN, s)];
    const entries = groupAgendaByHour(items);
    expect(entries).toHaveLength(2);
  });

  it("passes poll entries through ungrouped and resets the running group", () => {
    const s = schedule({ id: "s1", name: "poller" });
    const items = [
      fire(DAY_BASE, s),
      { kind: "poll" as const, schedule: schedule({ id: "s2", name: "gh-poll" }) },
      fire(DAY_BASE + 5 * MIN, s),
    ];
    const entries = groupAgendaByHour(items);
    expect(entries).toHaveLength(3);
    expect(entries[1].kind).toBe("poll");
    expect(entries[2].kind === "agendaGroup" && entries[2].count).toBe(1);
  });

  it("returns an empty list for an empty day", () => {
    expect(groupAgendaByHour([])).toEqual([]);
  });
});

// ─── truncateMiddle ───────────────────────────────────────────────────────

describe("truncateMiddle", () => {
  it("leaves short names untouched", () => {
    expect(truncateMiddle("nightly-build")).toBe("nightly-build");
  });

  it("truncates long names in the middle, preserving prefix and suffix", () => {
    const long = "extremely-long-schedule-name-that-does-not-fit-in-a-chip";
    const out = truncateMiddle(long, 20);
    expect(out.length).toBeLessThanOrEqual(21); // 20 + ellipsis rounding
    expect(out).toContain("…");
    expect(long.startsWith(out.split("…")[0])).toBe(true);
    expect(long.endsWith(out.split("…")[1])).toBe(true);
  });
});

// ─── Source contract — agenda week structure ─────────────────────────────

describe("SchedulesCalendar — week view is an agenda, not a time grid", () => {
  it("has a week branch distinct from the day branch", () => {
    expect(SRC).toMatch(/mode === "week" \? \(/);
  });

  it("week branch renders 7 equal columns via grid-cols-7, not an hour-row loop", () => {
    const weekBranchStart = SRC.indexOf('mode === "week" ? (');
    const dayBranchStart = SRC.indexOf(") : (", weekBranchStart);
    const weekBranch = SRC.slice(weekBranchStart, dayBranchStart);
    expect(weekBranch).toContain("grid-cols-7");
    expect(weekBranch).not.toContain("Array.from({ length: 24 }");
    expect(weekBranch).not.toContain("allDay");
  });

  it("uses the empty-day placeholder and the range badge in the week branch", () => {
    const weekBranchStart = SRC.indexOf('mode === "week" ? (');
    const dayBranchStart = SRC.indexOf(") : (", weekBranchStart);
    const weekBranch = SRC.slice(weekBranchStart, dayBranchStart);
    expect(weekBranch).toContain('t("emptyDay")');
    expect(weekBranch).toContain("groupAgendaByHour");
  });

  it("day view keeps its hour grid untouched", () => {
    const dayBranchStart = SRC.indexOf("Horizontal scroll lives HERE");
    const detailStripStart = SRC.indexOf("{/* Day detail strip */}");
    const dayBranch = SRC.slice(dayBranchStart, detailStripStart);
    expect(dayBranch).toContain("Array.from({ length: 24 }");
    expect(dayBranch).toContain("HOUR_ROW_PX");
  });
});
