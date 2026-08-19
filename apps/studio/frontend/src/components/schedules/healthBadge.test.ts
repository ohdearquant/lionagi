/**
 * scheduleHealthBadge — the display mapping from the server-computed health
 * verdict to what a badge renders. The backend derives health_state from
 * cadence + recorded schedule_runs rows (never from next_fire_at); this is a
 * pure passthrough that must not re-derive or override that verdict.
 */
import { describe, it, expect } from "vitest";
import { scheduleHealthBadge } from "./data";
import type { ScheduleSummary } from "@/lib/types";

function schedule(overrides: Partial<ScheduleSummary> = {}): ScheduleSummary {
  return {
    id: "sched-1",
    name: "nightly-build",
    description: null,
    enabled: 1,
    trigger_type: "interval",
    cron_expr: null,
    interval_sec: 300,
    github_repo: null,
    poll_interval_sec: null,
    action_kind: "agent",
    action_model: null,
    action_agent: null,
    action_playbook: null,
    action_project: null,
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

describe("scheduleHealthBadge", () => {
  it("is hidden when the server sends no health_state (older payload)", () => {
    expect(scheduleHealthBadge(schedule({ health_state: undefined }))).toEqual({ kind: "hidden" });
  });

  it("is hidden for a disabled schedule — the existing Paused pill already covers it", () => {
    const s = schedule({ health_state: "disabled" });
    expect(scheduleHealthBadge(s)).toEqual({ kind: "hidden" });
  });

  it("carries the never-fired-since timestamp for a schedule with zero executed runs", () => {
    const s = schedule({ health_state: "never-fired", health_since: 1_700_000_000 });
    expect(scheduleHealthBadge(s)).toEqual({ kind: "never-fired", sinceMs: 1_700_000_000_000 });
  });

  it("carries no timestamp for no-evidence — retention/bounds prevent proving anything, not a known since", () => {
    const s = schedule({ health_state: "no-evidence", health_since: 1_700_000_000 });
    expect(scheduleHealthBadge(s)).toEqual({ kind: "no-evidence" });
  });

  it("carries last outcome + when for healthy", () => {
    const s = schedule({
      health_state: "healthy",
      health_last_outcome: "completed",
      health_last_outcome_at: 1_700_000_000,
    });
    expect(scheduleHealthBadge(s)).toEqual({
      kind: "healthy",
      outcome: "completed",
      outcomeAtMs: 1_700_000_000_000,
    });
  });

  it("carries last outcome + when for failing", () => {
    const s = schedule({
      health_state: "failing",
      health_last_outcome: "failed",
      health_last_outcome_at: 1_700_000_000,
    });
    expect(scheduleHealthBadge(s)).toEqual({
      kind: "failing",
      outcome: "failed",
      outcomeAtMs: 1_700_000_000_000,
    });
  });

  it("still surfaces the last (stale) outcome for overdue — the staleness is the signal, not a missing outcome", () => {
    const s = schedule({
      health_state: "overdue",
      health_last_outcome: "completed",
      health_last_outcome_at: 1_700_000_000,
    });
    expect(scheduleHealthBadge(s)).toEqual({
      kind: "overdue",
      outcome: "completed",
      outcomeAtMs: 1_700_000_000_000,
    });
  });

  it("is hidden (not a crash) for a health_state the client doesn't recognize", () => {
    const s = schedule({
      health_state: "surprise-new-state" as unknown as ScheduleSummary["health_state"],
    });
    expect(scheduleHealthBadge(s)).toEqual({ kind: "hidden" });
  });

  it("null outcome fields pass through as null rather than throwing", () => {
    const s = schedule({
      health_state: "healthy",
      health_last_outcome: null,
      health_last_outcome_at: null,
    });
    expect(scheduleHealthBadge(s)).toEqual({ kind: "healthy", outcome: null, outcomeAtMs: null });
  });
});
