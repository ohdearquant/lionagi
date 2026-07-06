/**
 * Pure grouping tests for the Recent history strip: consecutive same-name/
 * same-outcome runs collapse into one expandable group; unrelated runs in
 * between break the run; orphaned (phantom-reaped) runs group separately
 * from genuine failures even when the raw status is the same "failed".
 */

import { describe, it, expect } from "vitest";
import { groupConsecutiveRecentRuns, groupSpanSec } from "./recentGroups";
import type { RecentGroup } from "./recentGroups";
import type { RunSummary } from "@/lib/types";

function makeRun(overrides: Partial<RunSummary> & { run_id: string; status: string }): RunSummary {
  const base: RunSummary = {
    run_id: overrides.run_id,
    status: overrides.status,
    playbook_name: null,
    agent_name: null,
    invocation_kind: null,
    show_topic: null,
    show_play_name: null,
    source_kind: "api",
    effective_health: null,
    last_message_at: null,
    invocation_id: null,
    started_at: null,
    ended_at: null,
  };
  return { ...base, ...overrides };
}

describe("groupConsecutiveRecentRuns", () => {
  it("a single run is its own group", () => {
    const groups = groupConsecutiveRecentRuns([
      makeRun({ run_id: "r1", status: "completed", playbook_name: "pr-merge-review" }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].runs).toHaveLength(1);
  });

  it("consecutive same name + status collapse into one group", () => {
    const runs = [
      makeRun({ run_id: "r3", status: "failed", playbook_name: "reviewer", ended_at: 300 }),
      makeRun({ run_id: "r2", status: "failed", playbook_name: "reviewer", ended_at: 200 }),
      makeRun({ run_id: "r1", status: "failed", playbook_name: "reviewer", ended_at: 100 }),
    ];
    const groups = groupConsecutiveRecentRuns(runs);
    expect(groups).toHaveLength(1);
    expect(groups[0].runs).toHaveLength(3);
    expect(groups[0].name).toBe("reviewer");
    expect(groups[0].displayStatus).toBe("failed");
    expect(groups[0].key).toBe("r3");
  });

  it("an unrelated run in between breaks the group — no non-adjacent merging", () => {
    const runs = [
      makeRun({ run_id: "r3", status: "failed", playbook_name: "reviewer", ended_at: 300 }),
      makeRun({ run_id: "mid", status: "completed", playbook_name: "codex-digest", ended_at: 250 }),
      makeRun({ run_id: "r1", status: "failed", playbook_name: "reviewer", ended_at: 100 }),
    ];
    const groups = groupConsecutiveRecentRuns(runs);
    expect(groups).toHaveLength(3);
    expect(groups.map((g) => g.runs.length)).toEqual([1, 1, 1]);
  });

  it("different status breaks the group even with the same name", () => {
    const runs = [
      makeRun({ run_id: "r2", status: "completed", playbook_name: "reviewer", ended_at: 200 }),
      makeRun({ run_id: "r1", status: "failed", playbook_name: "reviewer", ended_at: 100 }),
    ];
    const groups = groupConsecutiveRecentRuns(runs);
    expect(groups).toHaveLength(2);
  });

  it("orphaned (phantom_reaped) failures group separately from genuine failures of the same name", () => {
    const runs = [
      makeRun({
        run_id: "r2",
        status: "failed",
        playbook_name: "reviewer",
        ended_at: 200,
        status_reason_summary: "phantom_reaped",
      }),
      makeRun({
        run_id: "r1",
        status: "failed",
        playbook_name: "reviewer",
        ended_at: 100,
        status_reason_summary: "AgentCrashed",
      }),
    ];
    const groups = groupConsecutiveRecentRuns(runs);
    expect(groups).toHaveLength(2);
    expect(groups[0].displayStatus).toBe("orphaned");
    expect(groups[1].displayStatus).toBe("failed");
  });

  it("consecutive orphaned failures collapse into one orphaned group", () => {
    const runs = [
      makeRun({
        run_id: "r2",
        status: "failed",
        playbook_name: "reviewer",
        ended_at: 200,
        status_reason_summary: "phantom_reaped",
      }),
      makeRun({
        run_id: "r1",
        status: "failed",
        playbook_name: "reviewer",
        ended_at: 100,
        status_reason_summary: "phantom_reaped",
      }),
    ];
    const groups = groupConsecutiveRecentRuns(runs);
    expect(groups).toHaveLength(1);
    expect(groups[0].displayStatus).toBe("orphaned");
    expect(groups[0].runs).toHaveLength(2);
  });

  it("empty input yields no groups", () => {
    expect(groupConsecutiveRecentRuns([])).toEqual([]);
  });
});

describe("groupSpanSec", () => {
  it("a single-run group has zero span", () => {
    const group: RecentGroup = {
      key: "r1",
      runs: [makeRun({ run_id: "r1", status: "failed", ended_at: 100 })],
      name: "reviewer",
      displayStatus: "failed",
    };
    expect(groupSpanSec(group)).toBe(0);
  });

  it("spans from the oldest to the newest run in the group", () => {
    const group: RecentGroup = {
      key: "r3",
      runs: [
        makeRun({ run_id: "r3", status: "failed", ended_at: 1300 }),
        makeRun({ run_id: "r2", status: "failed", ended_at: 1200 }),
        makeRun({ run_id: "r1", status: "failed", ended_at: 1000 }),
      ],
      name: "reviewer",
      displayStatus: "failed",
    };
    expect(groupSpanSec(group)).toBe(300);
  });
});
