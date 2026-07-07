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

// ─── Source contract — "+N more" keyboard reachability ───────────────────

describe("SchedulesCalendar — '+N more' is keyboard-reachable", () => {
  const dayBranchStart = SRC.indexOf("Horizontal scroll lives HERE");
  const detailStripStart = SRC.indexOf("{/* Day detail strip */}");
  const dayBranch = SRC.slice(dayBranchStart, detailStripStart);
  const hourCellStart = dayBranch.indexOf(
    "{hourColumns.map((d) => {",
    dayBranch.indexOf("Hour grid"),
  );
  const hourCellBranch = dayBranch.slice(hourCellStart);
  const moreButtonStart = hourCellBranch.indexOf('aria-label={`${t("more"');
  const moreButtonEnd = hourCellBranch.indexOf("</button>", moreButtonStart) + "</button>".length;
  const moreButtonTagStart = hourCellBranch.lastIndexOf("<button", moreButtonStart);
  const moreButton = hourCellBranch.slice(moreButtonTagStart, moreButtonEnd);

  it('the hour-cell outer wrapper is not an interactive role="button" element', () => {
    const wrapperStart = hourCellBranch.indexOf("<div\n");
    const wrapperEnd = hourCellBranch.indexOf(">", wrapperStart);
    const wrapperTag = hourCellBranch.slice(wrapperStart, wrapperEnd);
    expect(wrapperTag).not.toContain('role="button"');
    expect(wrapperTag).not.toContain("tabIndex");
  });

  it('renders a real <button type="button"> for the overflow affordance', () => {
    expect(moreButton).toContain("<button");
    expect(moreButton).toContain('type="button"');
  });

  it('is not nested inside another role="button" element', () => {
    expect(hourCellBranch.slice(0, moreButtonTagStart)).not.toContain('role="button"');
  });

  it("has an aria-label mentioning the day view", () => {
    expect(moreButton).toMatch(
      /aria-label=\{`\$\{t\("more", \{ count: overflow \}\)\} \$\{t\("viewDay"\)\}`\}/,
    );
  });

  it("stops Enter/Space propagation so no ancestor can consume the key", () => {
    const onKeyDownStart = moreButton.indexOf("onKeyDown");
    const onKeyDownBlock = moreButton.slice(
      onKeyDownStart,
      moreButton.indexOf("}}", onKeyDownStart),
    );
    expect(onKeyDownBlock).toContain('"Enter"');
    expect(onKeyDownBlock).toContain('" "');
    expect(onKeyDownBlock).toContain("stopPropagation");
  });

  it("clicking it switches to day mode, anchors to the date, and selects the day", () => {
    expect(moreButton).toContain('switchMode("day")');
    expect(moreButton).toContain("setAnchor(startOfDay(d))");
    expect(moreButton).toContain("setSelectedDay(dk)");
  });
});

// ─── Source contract — sticky gutter and header ──────────────────────────

describe("SchedulesCalendar — sticky hour gutter and date headers", () => {
  it("defines sticky style constants for the header, corner, and gutter", () => {
    expect(SRC).toContain("const STICKY_GUTTER_STYLE");
    expect(SRC).toContain("const STICKY_HEADER_STYLE");
    expect(SRC).toContain("const STICKY_CORNER_STYLE");
    expect(SRC).toMatch(
      /STICKY_GUTTER_STYLE:\s*CSSProperties\s*=\s*\{\s*position:\s*"sticky",\s*left:\s*0/,
    );
    expect(SRC).toMatch(
      /STICKY_HEADER_STYLE:\s*CSSProperties\s*=\s*\{\s*position:\s*"sticky",\s*top:\s*0/,
    );
  });

  it("applies the sticky header style to the column-header row", () => {
    const headerRowStart = SRC.indexOf("Column header: gutter spacer + day headers.");
    const headerRowEnd = SRC.indexOf("{hourColumns.map((d) => {", headerRowStart);
    const headerRow = SRC.slice(headerRowStart, headerRowEnd);
    expect(headerRow).toContain("...STICKY_HEADER_STYLE");
  });

  it("applies the sticky corner style to the header's leading spacer", () => {
    const headerRowStart = SRC.indexOf("Column header: gutter spacer + day headers.");
    const headerRowEnd = SRC.indexOf("{hourColumns.map((d) => {", headerRowStart);
    const headerRow = SRC.slice(headerRowStart, headerRowEnd);
    expect(headerRow).toContain("style={STICKY_CORNER_STYLE}");
  });

  it("applies the sticky gutter style to the all-day leading gutter", () => {
    const allDayStart = SRC.indexOf("All-day row — poll indicators");
    const allDayHeaderEnd = SRC.indexOf('{t("allDay")}', allDayStart);
    const allDayGutter = SRC.slice(allDayStart, allDayHeaderEnd);
    expect(allDayGutter).toContain("style={STICKY_GUTTER_STYLE}");
  });

  it("applies the sticky gutter style to every hour label gutter cell", () => {
    const hourGridStart = SRC.indexOf("Hour grid — scrollable");
    const hourLabelEnd = SRC.indexOf('padStart(2, "0")}:00', hourGridStart);
    const hourLabel = SRC.slice(hourGridStart, hourLabelEnd);
    expect(hourLabel).toContain("...STICKY_GUTTER_STYLE");
  });

  it("pins the sticky header to one bounded viewport it shares with the all-day row and hour rows, not the page", () => {
    const dayBranchStart = SRC.indexOf("Horizontal scroll lives HERE");
    const detailStripStart = SRC.indexOf("{/* Day detail strip */}");
    const dayBranch = SRC.slice(dayBranchStart, detailStripStart);

    // Exactly one scroll container for the whole day/week grid — no separate
    // outer (horizontal-only) wrapper plus inner (vertical-only) hour body.
    // That split is what let the header's sticky ancestor diverge from the
    // page's actual vertical scroller in Chrome.
    const overflowAutoClassMatches =
      dayBranch.match(/className="[^"]*\boverflow-auto\b[^"]*"/g) ?? [];
    expect(overflowAutoClassMatches).toHaveLength(1);
    expect(dayBranch).not.toContain("overflow-x-auto");
    expect(dayBranch).not.toContain("overflow-y-auto");

    // The single scroll container carries hourGridRef directly, and both the
    // header and the all-day row live inside it (not outside, not in a
    // separately-scrolling child).
    expect(dayBranch).toMatch(/ref=\{hourGridRef\}[^>]*overflow-auto/s);
    expect(dayBranch).toContain("ref={allDayRowRef}");

    // No claim that the header pins against page-level scrolling — it pins
    // against this bounded container instead.
    expect(dayBranch).not.toMatch(/stay visible while the page scrolls/);
  });
});
