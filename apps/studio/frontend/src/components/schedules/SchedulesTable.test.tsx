/**
 * SchedulesTable tests, matching the project's existing style for this
 * feature (see SchedulesCalendar.test.tsx): pure logic gets real unit tests;
 * component wiring gets source-contract assertions since this project has
 * no @testing-library/react.
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { sortByNextFire } from "./SchedulesTable";
import type { ScheduleSummary } from "@/lib/types";

const TABLE_FILE = path.resolve(__dirname, "SchedulesTable.tsx");
const SRC = fs.readFileSync(TABLE_FILE, "utf-8");

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

// ─── sortByNextFire ─────────────────────────────────────────────────────────

describe("sortByNextFire — next-fire column sort", () => {
  it("sorts ascending by next_fire_at (soonest first)", () => {
    const a = schedule({ id: "a", name: "a", next_fire_at: 300 });
    const b = schedule({ id: "b", name: "b", next_fire_at: 100 });
    const c = schedule({ id: "c", name: "c", next_fire_at: 200 });
    expect(sortByNextFire([a, b, c], "asc").map((s) => s.id)).toEqual(["b", "c", "a"]);
  });

  it("sorts descending by next_fire_at when toggled", () => {
    const a = schedule({ id: "a", name: "a", next_fire_at: 300 });
    const b = schedule({ id: "b", name: "b", next_fire_at: 100 });
    const c = schedule({ id: "c", name: "c", next_fire_at: 200 });
    expect(sortByNextFire([a, b, c], "desc").map((s) => s.id)).toEqual(["a", "c", "b"]);
  });

  it("always sorts schedules with no next_fire_at to the bottom, in both directions", () => {
    const scheduled = schedule({ id: "scheduled", name: "z-scheduled", next_fire_at: 100 });
    const unscheduled = schedule({ id: "unscheduled", name: "a-unscheduled", next_fire_at: null });
    expect(sortByNextFire([unscheduled, scheduled], "asc").map((s) => s.id)).toEqual([
      "scheduled",
      "unscheduled",
    ]);
    expect(sortByNextFire([unscheduled, scheduled], "desc").map((s) => s.id)).toEqual([
      "scheduled",
      "unscheduled",
    ]);
  });

  it("breaks ties among unscheduled rows by name", () => {
    const b = schedule({ id: "b", name: "bravo", next_fire_at: null });
    const a = schedule({ id: "a", name: "alpha", next_fire_at: null });
    expect(sortByNextFire([b, a], "asc").map((s) => s.id)).toEqual(["a", "b"]);
  });

  it("does not mutate the input array", () => {
    const list = [
      schedule({ id: "a", next_fire_at: 200 }),
      schedule({ id: "b", next_fire_at: 100 }),
    ];
    const copy = [...list];
    sortByNextFire(list, "asc");
    expect(list).toEqual(copy);
  });
});

// ─── Source contract — one flat table, real wiring, no kanban ──────────────

describe("SchedulesTable — source contract", () => {
  it("renders exactly one <table>, not per-lane columns", () => {
    expect(SRC.match(/<table/g)?.length).toBe(1);
  });

  it("wires EnabledToggle with stopPropagation so it doesn't also open the row", () => {
    const cellStart = SRC.indexOf("<EnabledToggle");
    const before = SRC.slice(Math.max(0, cellStart - 300), cellStart);
    expect(before).toContain("stopPropagation");
  });

  it("uses StatusPill with the session taxonomy for the last-run cell", () => {
    expect(SRC).toContain('taxonomy="session"');
  });

  it("classifies failed-run errors instead of rendering the raw error_detail inline", () => {
    expect(SRC).toContain("classifyError(run.error_detail");
    expect(SRC).not.toMatch(/\{run\.error_detail\}/);
  });

  it("has a sortable Next fire header wired to the sort toggle", () => {
    expect(SRC).toContain("table.colNextFire");
    expect(SRC).toContain("setSortDir");
  });

  it("row click opens the schedule detail via onOpen", () => {
    expect(SRC).toContain("onClick={() => onOpen(schedule.id)}");
  });
});
