import { describe, it, expect } from "vitest";
import { boardReducer, initialBoardState } from "./boardReducer";
import type { BoardState } from "./boardReducer";
import type { RunSummary } from "@/lib/types";
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
): BoardState {
  return boardReducer(state, { type: "DATA_OK", runs, invocations, nowSec });
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

  it("sorts failed before stale before stuck before gated", () => {
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
    expect(reasons[0]).toBe("failed");
    expect(reasons[1]).toBe("stale");
    expect(reasons[2]).toBe("stuck");
    expect(reasons[3]).toBe("gated");
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
