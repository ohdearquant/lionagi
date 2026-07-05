import { describe, it, expect } from "vitest";
import { boardReducer, initialBoardState } from "./boardReducer";
import type { BoardState } from "./boardReducer";
import type { RunSummary, ScheduleSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";

// ─── Helpers ──────────────────────────────────────────────────────────────────

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

function makeInvocation(
  overrides: Partial<InvocationSummary> & { id: string; status: string; skill: string },
): InvocationSummary {
  return {
    plugin: null,
    prompt: null,
    started_at: 0,
    ended_at: null,
    session_count: 0,
    created_at: 0,
    updated_at: 0,
    node_metadata: null,
    project: null,
    project_source: null,
    ...overrides,
  };
}

function dispatchOk(
  state: BoardState,
  runs: RunSummary[],
  invocations: InvocationSummary[] = [],
  nowSec = 1_000_000,
  schedules: ScheduleSummary[] | null = null,
): BoardState {
  return boardReducer(state, { type: "DATA_OK", runs, invocations, schedules, nowSec });
}

function makeSchedule(
  overrides: Partial<ScheduleSummary> & { id: string; name: string },
): ScheduleSummary {
  const base: ScheduleSummary = {
    id: overrides.id,
    name: overrides.name,
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
  };
  return { ...base, ...overrides };
}

// ─── Three distinct non-data states ──────────────────────────────────────────

describe("boardReducer — three distinct non-data states", () => {
  it("starts in loading state", () => {
    const s = initialBoardState();
    expect(s.dataState).toBe("loading");
    expect(s.errorMessage).toBeNull();
    expect(s.lastUpdatedMs).toBeNull();
  });

  it("transitions loading → live on DATA_OK", () => {
    const s = dispatchOk(initialBoardState(), []);
    expect(s.dataState).toBe("live");
  });

  it("transitions live → stale on MARK_STALE", () => {
    const live = dispatchOk(initialBoardState(), []);
    const stale = boardReducer(live, { type: "MARK_STALE" });
    expect(stale.dataState).toBe("stale");
  });

  it("does not transition loading → stale (watchdog must not clobber loading)", () => {
    const s = boardReducer(initialBoardState(), { type: "MARK_STALE" });
    // loading should remain loading — MARK_STALE only acts on "live"
    expect(s.dataState).toBe("loading");
  });

  it("transitions any state → error on DATA_ERROR", () => {
    const s = boardReducer(initialBoardState(), {
      type: "DATA_ERROR",
      message: "network failure",
    });
    expect(s.dataState).toBe("error");
    expect(s.errorMessage).toBe("network failure");
  });

  it("does not clobber error state with MARK_STALE", () => {
    const errored = boardReducer(initialBoardState(), {
      type: "DATA_ERROR",
      message: "gone",
    });
    const after = boardReducer(errored, { type: "MARK_STALE" });
    expect(after.dataState).toBe("error");
  });

  it("stale → live on next DATA_OK", () => {
    const live = dispatchOk(initialBoardState(), []);
    const stale = boardReducer(live, { type: "MARK_STALE" });
    const backToLive = dispatchOk(stale, []);
    expect(backToLive.dataState).toBe("live");
  });

  it("updates lastUpdatedMs on DATA_OK", () => {
    const before = Date.now();
    const s = dispatchOk(initialBoardState(), []);
    expect(s.lastUpdatedMs).not.toBeNull();
    expect(s.lastUpdatedMs!).toBeGreaterThanOrEqual(before);
  });

  it("does not update lastUpdatedMs on MARK_STALE", () => {
    const live = dispatchOk(initialBoardState(), []);
    const ts = live.lastUpdatedMs;
    const stale = boardReducer(live, { type: "MARK_STALE" });
    expect(stale.lastUpdatedMs).toBe(ts);
  });
});

// ─── Attention queue derivation ───────────────────────────────────────────────

describe("boardReducer — attention queue derivation", () => {
  it("empty when no runs/invocations", () => {
    const s = dispatchOk(initialBoardState(), []);
    expect(s.attentionItems).toHaveLength(0);
  });

  it("failed runs appear in attention queue", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "failed", started_at: 1_000_000 - 600 }),
    ]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("failed");
    expect(s.attentionItems[0].kind).toBe("run");
  });

  it("failures older than 24h are excluded — they belong to History", () => {
    const nowSec = 1_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [
        makeRun({ run_id: "old", status: "failed", ended_at: nowSec - 25 * 3600 }),
        makeRun({ run_id: "recent", status: "failed", ended_at: nowSec - 3600 }),
      ],
      [],
      nowSec,
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].id).toBe("run:recent");
  });

  it("undated failures are excluded — age unknown is not actionable", () => {
    const s = dispatchOk(initialBoardState(), [makeRun({ run_id: "undated", status: "failed" })]);
    expect(s.attentionItems).toHaveLength(0);
  });

  it("running + stale health appears in attention queue", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "running", effective_health: "stale" }),
    ]);
    expect(s.attentionItems[0].reason).toBe("stale");
  });

  it("stale health does not demote a stuck run — stuck wins", () => {
    const nowSec = 2_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [
        makeRun({
          run_id: "r1",
          status: "running",
          effective_health: "stale",
          started_at: nowSec - 4000,
        }),
      ],
      [],
      nowSec,
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("stuck");
  });

  it("stale health does not demote a gated run — gated wins", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "needs_review", effective_health: "stale" }),
    ]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("gated");
  });

  it("stuck run (elapsed > threshold) appears in attention queue", () => {
    const nowSec = 2_000_000;
    const startedAt = nowSec - 4000; // 4000s > 3600s threshold
    const s = dispatchOk(
      initialBoardState(),
      [makeRun({ run_id: "r1", status: "running", started_at: startedAt })],
      [],
      nowSec,
    );
    expect(s.attentionItems[0].reason).toBe("stuck");
  });

  it("gated invocations appear in attention queue", () => {
    const s = dispatchOk(
      initialBoardState(),
      [],
      [makeInvocation({ id: "i1", status: "gated", skill: "code-review" })],
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("gated");
    expect(s.attentionItems[0].kind).toBe("invocation");
  });

  it("sorts gated before stuck before failed before stale", () => {
    const nowSec = 2_000_000;
    const stuckStart = nowSec - 4000;
    const s = dispatchOk(
      initialBoardState(),
      [
        makeRun({ run_id: "stuck", status: "running", started_at: stuckStart }),
        makeRun({ run_id: "failed", status: "failed", started_at: nowSec - 600 }),
        makeRun({ run_id: "gated", status: "gated" }),
        makeRun({ run_id: "stale", status: "running", effective_health: "stale" }),
      ],
      [],
      nowSec,
    );
    const reasons = s.attentionItems.map((i) => i.reason);
    expect(reasons[0]).toBe("gated");
    expect(reasons[1]).toBe("stuck");
    expect(reasons[2]).toBe("failed");
    expect(reasons[3]).toBe("stale");
  });

  it("deduplicates: a run that matches multiple criteria appears once (worst reason)", () => {
    const nowSec = 2_000_000;
    const stuckStart = nowSec - 4000;
    // Same run: failed AND stuck — should appear once as "failed"
    const s = dispatchOk(
      initialBoardState(),
      [
        makeRun({
          run_id: "r1",
          status: "failed",
          started_at: stuckStart,
          effective_health: "stale",
        }),
      ],
      [],
      nowSec,
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("failed");
  });
});

// ─── Live board derivation ────────────────────────────────────────────────────

describe("boardReducer — active/recent derivation", () => {
  it("only running runs appear on live board", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "running" }),
      makeRun({ run_id: "r2", status: "completed" }),
      makeRun({ run_id: "r3", status: "failed" }),
    ]);
    expect(s.activeRuns).toHaveLength(1);
    expect(s.activeRuns[0].run_id).toBe("r1");
  });

  it("recentRuns contains terminal runs, capped at 10", () => {
    const runs = Array.from({ length: 15 }, (_, i) =>
      makeRun({ run_id: `r${i}`, status: "completed", started_at: 1000 + i }),
    );
    const s = dispatchOk(initialBoardState(), runs);
    expect(s.recentRuns).toHaveLength(10);
  });

  it("recentRuns sorted most-recent first", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "old", status: "completed", started_at: 100 }),
      makeRun({ run_id: "new", status: "completed", started_at: 999 }),
    ]);
    expect(s.recentRuns[0].run_id).toBe("new");
  });

  it("running items not in recentRuns", () => {
    const s = dispatchOk(initialBoardState(), [makeRun({ run_id: "r1", status: "running" })]);
    expect(s.recentRuns).toHaveLength(0);
  });
});

// ─── TICK action ─────────────────────────────────────────────────────────────

describe("boardReducer — TICK", () => {
  it("updates nowSec without touching data", () => {
    const live = dispatchOk(initialBoardState(), [makeRun({ run_id: "r1", status: "running" })]);
    const ticked = boardReducer(live, { type: "TICK", nowSec: 9_999_999 });
    expect(ticked.nowSec).toBe(9_999_999);
    expect(ticked.activeRuns).toHaveLength(1);
    expect(ticked.dataState).toBe("live");
  });
});

// ─── Schedule failure streaks ─────────────────────────────────────────────────

describe("boardReducer — schedule failure streaks", () => {
  it("surfaces a streak row when consecutive_failures reaches the threshold", () => {
    const sched = makeSchedule({
      id: "sch-1",
      name: "nightly-sync",
      consecutive_failures: 3,
      last_status: "failed",
      last_fired_at: 999_000,
    });
    const s = dispatchOk(initialBoardState(), [], [], 1_000_000, [sched]);
    expect(s.attentionItems).toHaveLength(1);
    const item = s.attentionItems[0];
    expect(item.reason).toBe("streak");
    expect(item.kind).toBe("schedule");
    expect(item.streakCount).toBe(3);
    expect(item.name).toBe("nightly-sync");
  });

  it("ignores schedules below the threshold", () => {
    const sched = makeSchedule({ id: "sch-1", name: "s", consecutive_failures: 2 });
    const s = dispatchOk(initialBoardState(), [], [], 1_000_000, [sched]);
    expect(s.attentionItems).toHaveLength(0);
  });

  it("ignores disabled schedules regardless of streak", () => {
    const sched = makeSchedule({
      id: "sch-1",
      name: "s",
      enabled: 0,
      consecutive_failures: 9,
    });
    const s = dispatchOk(initialBoardState(), [], [], 1_000_000, [sched]);
    expect(s.attentionItems).toHaveLength(0);
  });

  it("orders streak rows above gated and failed items", () => {
    const failedRun = makeRun({
      run_id: "r1",
      status: "failed",
      started_at: 999_990,
      ended_at: 999_995,
    });
    const gatedRun = makeRun({ run_id: "r2", status: "needs_review", started_at: 999_990 });
    const sched = makeSchedule({ id: "sch-1", name: "s", consecutive_failures: 4 });
    const s = dispatchOk(initialBoardState(), [failedRun, gatedRun], [], 1_000_000, [sched]);
    expect(s.attentionItems.map((i) => i.reason)).toEqual(["streak", "gated", "failed"]);
  });

  it("keeps the last-known schedules when the schedules fetch degrades to null", () => {
    const sched = makeSchedule({ id: "sch-1", name: "s", consecutive_failures: 5 });
    let s = dispatchOk(initialBoardState(), [], [], 1_000_000, [sched]);
    expect(s.attentionItems).toHaveLength(1);
    s = dispatchOk(s, [], [], 1_000_001, null);
    expect(s.schedules).toHaveLength(1);
    expect(s.attentionItems).toHaveLength(1);
  });

  it("clears the streak row once a fresh fetch reports recovery", () => {
    const failing = makeSchedule({ id: "sch-1", name: "s", consecutive_failures: 3 });
    const recovered = makeSchedule({
      id: "sch-1",
      name: "s",
      consecutive_failures: 0,
      last_status: "completed",
    });
    let s = dispatchOk(initialBoardState(), [], [], 1_000_000, [failing]);
    expect(s.attentionItems).toHaveLength(1);
    s = dispatchOk(s, [], [], 1_000_001, [recovered]);
    expect(s.attentionItems).toHaveLength(0);
  });
});
