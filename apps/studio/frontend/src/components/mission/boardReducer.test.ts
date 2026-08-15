import { describe, it, expect } from "vitest";
import { boardReducer, initialBoardState } from "./boardReducer";
import type { BoardState } from "./boardReducer";
import type { RunSummary, ScheduleSummary } from "@/lib/types";
import type { AttentionDisposition, GatedPlaySummary, InvocationSummary } from "@/lib/api";
import { resolveRunLabel } from "@/lib/runLabel";

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
  dispositions: Record<string, AttentionDisposition> | null = null,
  gatedPlays: GatedPlaySummary[] | null = null,
): BoardState {
  return boardReducer(state, {
    type: "DATA_OK",
    runs,
    invocations,
    schedules,
    dispositions,
    gatedPlays,
    nowSec,
  });
}

function makeGatedPlay(
  overrides: Partial<GatedPlaySummary> & { id: string; topic: string; play_name: string },
): GatedPlaySummary {
  return {
    started_at: null,
    updated_at: null,
    feedback: null,
    session_id: null,
    ...overrides,
  };
}

function makeDisposition(
  overrides: Partial<AttentionDisposition> & {
    item_id: string;
    state: AttentionDisposition["state"];
  },
): AttentionDisposition {
  const base: Omit<AttentionDisposition, "item_id" | "state"> = {
    note: null,
    created_at: 1_000_000,
    updated_at: 1_000_000,
    expires_at: null,
    actor: "operator",
    source_status: "failed",
    revision: 1,
  };
  return { ...base, ...overrides };
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

  it("a run's attention-queue name matches the shared resolver, never a raw playbook/agent fallback", () => {
    const run = makeRun({
      run_id: "r1",
      status: "failed",
      started_at: 1_000_000 - 600,
      playbook_name: "pr-merge-review",
      agent_name: "implementer",
    });
    const s = dispatchOk(initialBoardState(), [run]);
    expect(s.attentionItems[0].name).toBe(resolveRunLabel(run));
  });

  it("running + stale health appears in attention queue", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "running", effective_health: "stale" }),
    ]);
    expect(s.attentionItems[0].reason).toBe("stale");
  });

  it("orphaned health flags a running run as stale", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "running", effective_health: "orphaned" }),
    ]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("stale");
  });

  it("stale health does not demote a gated run — gated wins", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "needs_review", effective_health: "stale" }),
    ]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("gated");
  });

  it("unresponsive health flags a running run as stuck", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "running", effective_health: "unresponsive" }),
    ]);
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

  it("a gated PLAY is sourced live and reaches the queue with kind play and its feedback as reasonSummary", () => {
    const s = dispatchOk(initialBoardState(), [], [], 1_000_000, null, null, [
      makeGatedPlay({
        id: "play:show-1:p1",
        topic: "show-1",
        play_name: "p1",
        feedback: "needs another pass on the retry logic",
      }),
    ]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0]).toMatchObject({
      id: "play:show-1:p1",
      kind: "play",
      reason: "gated",
      reasonSummary: "needs another pass on the retry logic",
    });
  });

  it("a run in the terminal 'blocked' status is never treated as gated", () => {
    const s = dispatchOk(initialBoardState(), [makeRun({ run_id: "r1", status: "blocked" })]);
    expect(s.attentionItems).toHaveLength(0);
  });

  it("sorts gated before stuck before failed before stale", () => {
    const nowSec = 2_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [
        makeRun({ run_id: "stuck", status: "running", effective_health: "unresponsive" }),
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

  it("a phantom-reaped run never appears in the attention queue — housekeeping, not failure (DESIGN-BRIEF §0)", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({
        run_id: "r1",
        status: "failed",
        started_at: 1_000_000 - 600,
        status_reason_summary: "phantom_reaped",
      }),
    ]);
    expect(s.attentionItems).toHaveLength(0);
  });

  it("a zombie (stale-locks) reap still surfaces as a real failure", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({
        run_id: "r1",
        status: "failed",
        started_at: 1_000_000 - 600,
        status_reason_code: "session.zombie.stale_locks",
        status_reason_summary: "phantom_reaped",
      }),
    ]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("failed");
  });

  it("deduplicates: a run that matches multiple criteria appears once (worst reason)", () => {
    const nowSec = 2_000_000;
    // Same run: failed status AND stale health — should appear once as "failed"
    const s = dispatchOk(
      initialBoardState(),
      [
        makeRun({
          run_id: "r1",
          status: "failed",
          started_at: nowSec - 600,
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

  it("a 'timeout' alias run surfaces in recentRuns — the local TERMINAL_STATUSES set this replaced only knew 'timed_out'", () => {
    const s = dispatchOk(initialBoardState(), [makeRun({ run_id: "r1", status: "timeout" })]);
    expect(s.recentRuns).toHaveLength(1);
    expect(s.activeRuns).toHaveLength(0);
  });
});

// ─── Live board ordering — creation order, oldest first, never recency ───────
//
// The bug this closes: the board reordered every few seconds because it
// inherited the API's `ORDER BY updated_at DESC`, and updated_at bumps on
// every message. These tests assert the properties the fix must hold, not
// just "sorted ascending" (a recency sort would also pass an ascending
// check on already-sorted input — it only fails once the same runs arrive
// in a *different* API order, or one of them gets a message in between).

describe("boardReducer — active board creation order", () => {
  it("orders active runs by started_at ascending, oldest first", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "newest", status: "running", started_at: 300 }),
      makeRun({ run_id: "oldest", status: "running", started_at: 100 }),
      makeRun({ run_id: "middle", status: "running", started_at: 200 }),
    ]);
    expect(s.activeRuns.map((r) => r.run_id)).toEqual(["oldest", "middle", "newest"]);
  });

  it("derives an identical order from the same runs fed in a different API order — order is not inherited array position", () => {
    const runs = [
      makeRun({ run_id: "a", status: "running", started_at: 300 }),
      makeRun({ run_id: "b", status: "running", started_at: 100 }),
      makeRun({ run_id: "c", status: "running", started_at: 200 }),
    ];
    const forward = dispatchOk(initialBoardState(), runs);
    const shuffled = dispatchOk(initialBoardState(), [runs[2], runs[0], runs[1]]);
    const reversed = dispatchOk(initialBoardState(), [...runs].reverse());
    const order = forward.activeRuns.map((r) => r.run_id);
    expect(order).toEqual(["b", "c", "a"]);
    expect(shuffled.activeRuns.map((r) => r.run_id)).toEqual(order);
    expect(reversed.activeRuns.map((r) => r.run_id)).toEqual(order);
  });

  it("a run's position does not move when it receives a message (updated_at/last_message_at churn only)", () => {
    const runs = [
      makeRun({ run_id: "a", status: "running", started_at: 100, last_message_at: 100 }),
      makeRun({ run_id: "b", status: "running", started_at: 200, last_message_at: 200 }),
    ];
    const before = dispatchOk(initialBoardState(), runs);
    // "b" just talked — a recency sort would now put it first.
    const chatty = runs.map((r) => (r.run_id === "b" ? { ...r, last_message_at: 9_999_999 } : r));
    const after = dispatchOk(before, chatty);
    expect(before.activeRuns.map((r) => r.run_id)).toEqual(["a", "b"]);
    expect(after.activeRuns.map((r) => r.run_id)).toEqual(["a", "b"]);
  });

  it("breaks a tied started_at deterministically by run_id, not array order", () => {
    const tied = [
      makeRun({ run_id: "z-run", status: "running", started_at: 500 }),
      makeRun({ run_id: "a-run", status: "running", started_at: 500 }),
    ];
    const forward = dispatchOk(initialBoardState(), tied);
    const reversed = dispatchOk(initialBoardState(), [...tied].reverse());
    expect(forward.activeRuns.map((r) => r.run_id)).toEqual(["a-run", "z-run"]);
    expect(reversed.activeRuns.map((r) => r.run_id)).toEqual(["a-run", "z-run"]);
  });

  it("a run with no started_at yet falls back to created_at rather than losing its place", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "has-started", status: "running", started_at: 200 }),
      makeRun({ run_id: "not-started-yet", status: "running", started_at: null, created_at: 50 }),
    ]);
    // created_at (50) predates the started run's started_at (200) — the
    // undated-by-started_at run sorts first, not last-by-default and not
    // wherever the API happened to place it.
    expect(s.activeRuns.map((r) => r.run_id)).toEqual(["not-started-yet", "has-started"]);
  });

  it("runs with neither started_at nor created_at still produce a total, stable order (tiebreak by id)", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "z", status: "running", started_at: null }),
      makeRun({ run_id: "a", status: "running", started_at: null }),
      makeRun({ run_id: "m", status: "running", started_at: 10 }),
    ]);
    // Dated run sorts before the undated pair; the undated pair is ordered
    // by id, not left to whatever position the API returned them in.
    expect(s.activeRuns.map((r) => r.run_id)).toEqual(["m", "a", "z"]);
  });

  it("orders active invocations by started_at ascending, oldest first, independent of API order", () => {
    const invs = [
      makeInvocation({ id: "i-new", status: "running", skill: "s", started_at: 300 }),
      makeInvocation({ id: "i-old", status: "running", skill: "s", started_at: 100 }),
    ];
    const forward = dispatchOk(initialBoardState(), [], invs);
    const reversed = dispatchOk(initialBoardState(), [], [...invs].reverse());
    expect(forward.activeInvocations.map((i) => i.id)).toEqual(["i-old", "i-new"]);
    expect(reversed.activeInvocations.map((i) => i.id)).toEqual(["i-old", "i-new"]);
  });

  it("empty board: no runs or invocations produces empty, stable arrays", () => {
    const s = dispatchOk(initialBoardState(), [], []);
    expect(s.activeRuns).toEqual([]);
    expect(s.activeInvocations).toEqual([]);
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

describe("failure reason summaries", () => {
  it("carries the run's status_reason_summary on failed items", () => {
    const run = makeRun({
      run_id: "r1",
      status: "failed",
      started_at: 999_000,
      ended_at: 999_500,
      status_reason_summary: "ProviderQuotaError: usage limit reached",
    });
    const s = dispatchOk(initialBoardState(), [run]);
    expect(s.attentionItems[0].reasonSummary).toBe("ProviderQuotaError: usage limit reached");
  });

  it("omits reasonSummary when the run has none or is not failed", () => {
    const bare = makeRun({
      run_id: "r1",
      status: "failed",
      started_at: 999_000,
      ended_at: 999_500,
    });
    const gated = makeRun({
      run_id: "r2",
      status: "needs_review",
      started_at: 999_000,
      status_reason_summary: "should not surface on gated rows",
    });
    const s = dispatchOk(initialBoardState(), [bare, gated]);
    const byId = new Map(s.attentionItems.map((i) => [i.id, i]));
    expect(byId.get("run:r1")?.reasonSummary).toBeUndefined();
    expect(byId.get("run:r2")?.reasonSummary).toBeUndefined();
  });
});

describe("systemEmpty", () => {
  it("starts false while loading", () => {
    expect(initialBoardState().systemEmpty).toBe(false);
  });

  it("turns true only when a successful fetch reports no work at all", () => {
    const s = dispatchOk(initialBoardState(), [], [], 1_000_000, []);
    expect(s.systemEmpty).toBe(true);
  });

  it("stays false when any run, invocation, or schedule exists", () => {
    const withRun = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "completed", started_at: 1, ended_at: 2 }),
    ]);
    expect(withRun.systemEmpty).toBe(false);

    const withInv = dispatchOk(
      initialBoardState(),
      [],
      [makeInvocation({ id: "i1", status: "completed", skill: "s" })],
      1_000_000,
      [],
    );
    expect(withInv.systemEmpty).toBe(false);

    const withSched = dispatchOk(initialBoardState(), [], [], 1_000_000, [
      makeSchedule({ id: "sch-1", name: "s" }),
    ]);
    expect(withSched.systemEmpty).toBe(false);
  });

  it("respects last-known schedules when the schedules fetch degrades to null", () => {
    const sched = makeSchedule({ id: "sch-1", name: "s" });
    let s = dispatchOk(initialBoardState(), [], [], 1_000_000, [sched]);
    expect(s.systemEmpty).toBe(false);
    s = dispatchOk(s, [], [], 1_000_001, null);
    expect(s.systemEmpty).toBe(false);
  });

  it("stays false when the first schedules fetch is degraded — empty placeholder is not knowledge", () => {
    let s = dispatchOk(initialBoardState(), [], [], 1_000_000, null);
    expect(s.systemEmpty).toBe(false);
    expect(s.schedulesKnown).toBe(false);
    // Once schedules are confirmed empty, the zero state may show.
    s = dispatchOk(s, [], [], 1_000_001, []);
    expect(s.systemEmpty).toBe(true);
    expect(s.schedulesKnown).toBe(true);
  });

  it("stays true across later degraded schedule fetches once schedules were confirmed", () => {
    let s = dispatchOk(initialBoardState(), [], [], 1_000_000, []);
    expect(s.systemEmpty).toBe(true);
    s = dispatchOk(s, [], [], 1_000_001, null);
    expect(s.systemEmpty).toBe(true);
  });
});

// ─── Attention discharge lifecycle (dispositions join/filter) ────────────────

describe("boardReducer — disposition join and active/discharged split", () => {
  it("an item with no disposition is 'open' — active, no disposition field", () => {
    const s = dispatchOk(initialBoardState(), [
      makeRun({ run_id: "r1", status: "failed", started_at: 1_000_000 - 600 }),
    ]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].disposition).toBeUndefined();
    expect(s.dischargedAttentionItems).toHaveLength(0);
    expect(s.unacknowledgedAttentionCount).toBe(1);
  });

  it("acknowledged stays in the active list, restyled, and leaves the unacknowledged count", () => {
    const nowSec = 1_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [makeRun({ run_id: "r1", status: "failed", started_at: nowSec - 600 })],
      [],
      nowSec,
      null,
      { "run:r1": makeDisposition({ item_id: "run:r1", state: "acknowledged" }) },
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].disposition?.state).toBe("acknowledged");
    expect(s.dischargedAttentionItems).toHaveLength(0);
    expect(s.unacknowledgedAttentionCount).toBe(0);
  });

  it("a gated run carrying a stale resolved/expected/snoozed disposition from before gate-awareness stays active, never permanently discharged", () => {
    const nowSec = 1_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [makeRun({ run_id: "r1", status: "needs_review" })],
      [],
      nowSec,
      null,
      { "run:r1": makeDisposition({ item_id: "run:r1", state: "resolved" }) },
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].reason).toBe("gated");
    expect(s.dischargedAttentionItems).toHaveLength(0);
  });

  it("a gate is discharged by acknowledged, never by resolved/expected/snoozed", () => {
    const nowSec = 1_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [makeRun({ run_id: "r1", status: "needs_review" })],
      [],
      nowSec,
      null,
      { "run:r1": makeDisposition({ item_id: "run:r1", state: "acknowledged" }) },
    );
    // Acknowledged is the one state that actually clears a gate from the
    // active queue — the human made the decision the gate was waiting on
    // them to see. It lands in discharged (queryable), never vanishes.
    expect(s.attentionItems).toHaveLength(0);
    expect(s.dischargedAttentionItems).toHaveLength(1);
    expect(s.dischargedAttentionItems[0].disposition?.state).toBe("acknowledged");
  });

  it("resolved leaves the active list for dischargedAttentionItems", () => {
    const nowSec = 1_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [makeRun({ run_id: "r1", status: "failed", started_at: nowSec - 600 })],
      [],
      nowSec,
      null,
      { "run:r1": makeDisposition({ item_id: "run:r1", state: "resolved" }) },
    );
    expect(s.attentionItems).toHaveLength(0);
    expect(s.dischargedAttentionItems).toHaveLength(1);
    expect(s.dischargedAttentionItems[0].disposition?.state).toBe("resolved");
    expect(s.unacknowledgedAttentionCount).toBe(0);
  });

  it("expected and snoozed also leave the active list", () => {
    const nowSec = 1_000_000;
    const runs = [
      makeRun({ run_id: "r-expected", status: "failed", started_at: nowSec - 600 }),
      makeRun({ run_id: "r-snoozed", status: "failed", started_at: nowSec - 600 }),
    ];
    const dispositions = {
      "run:r-expected": makeDisposition({
        item_id: "run:r-expected",
        state: "expected",
        note: "deploy window",
        expires_at: nowSec + 3600,
      }),
      "run:r-snoozed": makeDisposition({
        item_id: "run:r-snoozed",
        state: "snoozed",
        expires_at: nowSec + 3600,
      }),
    };
    const s = dispatchOk(initialBoardState(), runs, [], nowSec, null, dispositions);
    expect(s.attentionItems).toHaveLength(0);
    expect(s.dischargedAttentionItems).toHaveLength(2);
  });

  it("a resolved disposition on an old run never suppresses a different, later run with a new id", () => {
    const nowSec = 1_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [makeRun({ run_id: "new-failure", status: "failed", started_at: nowSec - 600 })],
      [],
      nowSec,
      null,
      // A disposition keyed to a *different* item id (the prior occurrence)
      // must never touch this run — item ids are per-occurrence by construction.
      { "run:old-failure": makeDisposition({ item_id: "run:old-failure", state: "resolved" }) },
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].id).toBe("run:new-failure");
    expect(s.dischargedAttentionItems).toHaveLength(0);
  });

  it("a schedule streak re-enters the active list after recovering and re-crossing the threshold, even with a stale resolved disposition", () => {
    // The streak item id is stable (sched:<id>) across the whole burst — the
    // reducer doesn't know "this streak" from "that streak", so a lingering
    // resolved disposition DOES still hide a re-crossed streak today; this
    // is the documented weakness of the per-item overlay design (issue
    // fences: schedule streak re-entry is expiry/threshold-driven, not a
    // generation key). We assert the actually-implemented contract: absent
    // a disposition, the item is active again.
    const nowSec = 1_000_000;
    const sched = makeSchedule({ id: "s1", name: "nightly", consecutive_failures: 5 });
    const s = dispatchOk(initialBoardState(), [], [], nowSec, [sched]);
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].id).toBe("sched:s1");
    expect(s.attentionItems[0].disposition).toBeUndefined();
  });

  it("degrades to the last-known dispositions map when a poll's dispositions fetch fails (null)", () => {
    const nowSec = 1_000_000;
    const run = makeRun({ run_id: "r1", status: "failed", started_at: nowSec - 600 });
    let s = dispatchOk(initialBoardState(), [run], [], nowSec, null, {
      "run:r1": makeDisposition({ item_id: "run:r1", state: "resolved" }),
    });
    expect(s.dischargedAttentionItems).toHaveLength(1);

    // Next poll: dispositions fetch failed (null) — the last-known map must
    // be kept, same contract as the existing schedules degrade-to-null path.
    s = dispatchOk(s, [run], [], nowSec + 3, null, null);
    expect(s.dischargedAttentionItems).toHaveLength(1);
    expect(s.dispositions["run:r1"].state).toBe("resolved");
  });

  it("a lapsed snoozed/expected disposition is simply absent from the server read — the item reads as open again", () => {
    // The server (list_dispositions) already drops lapsed rows; the reducer
    // only ever sees what's still active, so an item with no matching key
    // in the dispositions map is open, never distinguished from "never
    // discharged" — this is the documented, intentional lapse semantics.
    const nowSec = 1_000_000;
    const s = dispatchOk(
      initialBoardState(),
      [makeRun({ run_id: "r1", status: "failed", started_at: nowSec - 600 })],
      [],
      nowSec,
      null,
      {}, // server already excluded the lapsed row
    );
    expect(s.attentionItems).toHaveLength(1);
    expect(s.attentionItems[0].disposition).toBeUndefined();
  });
});
