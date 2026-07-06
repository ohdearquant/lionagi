/**
 * nextFireState / sortSchedulesForCards — the "a paused schedule never fires"
 * contract. A disabled schedule keeps a stale next_fire_at in the DB; every
 * surface derives its displayed fire state through nextFireState so a stopped
 * schedule can never render as upcoming or overdue.
 */
import { describe, it, expect } from "vitest";
import { nextFireState, sortSchedulesForCards } from "./data";
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

// A realistic 2026 ms epoch — safely above toMs's 1e12 seconds/ms threshold so
// that NOW ± small deltas stay in the millisecond range.
const NOW = 1_750_000_000_000;

describe("nextFireState — enabled is checked first", () => {
  it("a disabled schedule is 'paused' even when it has a stale next_fire_at in the past", () => {
    const s = schedule({ enabled: 0, next_fire_at: NOW - 5 * 86_400_000 });
    expect(nextFireState(s, NOW).kind).toBe("paused");
  });

  it("a disabled schedule is 'paused' even when its next_fire_at is in the future", () => {
    const s = schedule({ enabled: 0, next_fire_at: NOW + 3_600_000 });
    expect(nextFireState(s, NOW).kind).toBe("paused");
  });

  it("a disabled github_poll schedule is 'paused', not 'watching'", () => {
    const s = schedule({ enabled: 0, trigger_type: "github_poll", github_repo: "o/r" });
    expect(nextFireState(s, NOW).kind).toBe("paused");
  });

  it("an enabled github_poll schedule is 'watching'", () => {
    const s = schedule({ enabled: 1, trigger_type: "github_poll", github_repo: "o/r" });
    expect(nextFireState(s, NOW).kind).toBe("watching");
  });

  it("an enabled schedule with no next_fire_at is 'unscheduled'", () => {
    const s = schedule({ enabled: 1, next_fire_at: null });
    expect(nextFireState(s, NOW).kind).toBe("unscheduled");
  });

  it("an enabled schedule fires in the future (not overdue, soon within the hour)", () => {
    const s = schedule({ enabled: 1, next_fire_at: NOW + 600_000 });
    const state = nextFireState(s, NOW);
    expect(state).toMatchObject({ kind: "fire", overdue: false, soon: true });
  });

  it("an enabled schedule past its next_fire_at is overdue", () => {
    const s = schedule({ enabled: 1, next_fire_at: NOW - 600_000 });
    const state = nextFireState(s, NOW);
    expect(state).toMatchObject({ kind: "fire", overdue: true });
  });

  it("normalizes epoch seconds to milliseconds", () => {
    const s = schedule({ enabled: 1, next_fire_at: Math.floor((NOW + 600_000) / 1000) });
    const state = nextFireState(s, NOW);
    if (state.kind !== "fire") throw new Error("expected fire");
    expect(state.fireMs).toBe(NOW + 600_000);
  });
});

describe("sortSchedulesForCards — live first, paused sink to the bottom", () => {
  it("orders enabled schedules before disabled ones", () => {
    const paused = schedule({ id: "paused", name: "a-paused", enabled: 0, next_fire_at: 100 });
    const live = schedule({ id: "live", name: "z-live", enabled: 1, next_fire_at: 500 });
    expect(sortSchedulesForCards([paused, live]).map((s) => s.id)).toEqual(["live", "paused"]);
  });

  it("within the same enabled state, sorts by soonest next_fire_at", () => {
    const a = schedule({ id: "a", enabled: 1, next_fire_at: 300 });
    const b = schedule({ id: "b", enabled: 1, next_fire_at: 100 });
    expect(sortSchedulesForCards([a, b]).map((s) => s.id)).toEqual(["b", "a"]);
  });

  it("sorts null next_fire_at to the bottom of its enabled group, by name", () => {
    const scheduled = schedule({ id: "scheduled", name: "z", enabled: 1, next_fire_at: 100 });
    const unscheduled = schedule({ id: "unscheduled", name: "a", enabled: 1, next_fire_at: null });
    expect(sortSchedulesForCards([unscheduled, scheduled]).map((s) => s.id)).toEqual([
      "scheduled",
      "unscheduled",
    ]);
  });
});
