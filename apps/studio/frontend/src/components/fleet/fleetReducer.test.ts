import { describe, it, expect } from "vitest";
import {
  fleetReducer,
  initialFleetState,
  terminalRecentRows,
  createHistoryPager,
} from "./fleetReducer";
import type { FleetState } from "./fleetReducer";
import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";
import { deriveDisplayStatus } from "@/lib/runStatus";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function makeRun(overrides: Partial<RunSummary> & { run_id: string; status: string }): RunSummary {
  const base: Omit<RunSummary, "run_id" | "status"> = {
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
    branch_count: 0,
    message_count: 0,
  };
  return { ...base, ...overrides };
}

function makeInvocation(
  overrides: Partial<InvocationSummary> & { id: string; status: string; skill: string },
): InvocationSummary {
  return {
    plugin: null,
    prompt: null,
    started_at: 1_000_000,
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
  state: FleetState,
  invocations: InvocationSummary[],
  runs: RunSummary[],
  nowSec = 1_000_000,
): FleetState {
  return fleetReducer(state, { type: "DATA_OK", invocations, runs, runsHasNext: false, nowSec });
}

// ─── Data state transitions ───────────────────────────────────────────────────

describe("fleetReducer — data state transitions", () => {
  it("starts in loading state", () => {
    const s = initialFleetState();
    expect(s.dataState).toBe("loading");
    expect(s.errorMessage).toBeNull();
    expect(s.lastUpdatedMs).toBeNull();
  });

  it("transitions loading → live on DATA_OK", () => {
    const s = dispatchOk(initialFleetState(), [], []);
    expect(s.dataState).toBe("live");
  });

  it("transitions live → stale on MARK_STALE", () => {
    const live = dispatchOk(initialFleetState(), [], []);
    const stale = fleetReducer(live, { type: "MARK_STALE" });
    expect(stale.dataState).toBe("stale");
  });

  it("does not transition loading → stale", () => {
    const s = fleetReducer(initialFleetState(), { type: "MARK_STALE" });
    expect(s.dataState).toBe("loading");
  });

  it("transitions to error on DATA_ERROR", () => {
    const s = fleetReducer(initialFleetState(), { type: "DATA_ERROR", message: "fail" });
    expect(s.dataState).toBe("error");
    expect(s.errorMessage).toBe("fail");
  });

  it("does not clobber error with MARK_STALE", () => {
    const err = fleetReducer(initialFleetState(), { type: "DATA_ERROR", message: "x" });
    const after = fleetReducer(err, { type: "MARK_STALE" });
    expect(after.dataState).toBe("error");
  });

  it("recovers from stale on DATA_OK", () => {
    const live = dispatchOk(initialFleetState(), [], []);
    const stale = fleetReducer(live, { type: "MARK_STALE" });
    const back = dispatchOk(stale, [], []);
    expect(back.dataState).toBe("live");
  });

  it("updates lastUpdatedMs on DATA_OK", () => {
    const before = Date.now();
    const s = dispatchOk(initialFleetState(), [], []);
    expect(s.lastUpdatedMs).not.toBeNull();
    expect(s.lastUpdatedMs!).toBeGreaterThanOrEqual(before);
  });

  it("TICK updates nowSec only", () => {
    const live = dispatchOk(initialFleetState(), [], []);
    const ticked = fleetReducer(live, { type: "TICK", nowSec: 9_999 });
    expect(ticked.nowSec).toBe(9_999);
    expect(ticked.dataState).toBe("live");
  });
});

// ─── Terminal exclusion ───────────────────────────────────────────────────────

describe("fleetReducer — terminal exclusion", () => {
  it("excludes completed runs", () => {
    const s = dispatchOk(initialFleetState(), [], [makeRun({ run_id: "r1", status: "completed" })]);
    expect(s.orgUnits).toHaveLength(0);
    expect(s.counts.agents).toBe(0);
  });

  it("excludes terminal invocations", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "i1", status: "done", skill: "review" })],
      [],
    );
    expect(s.orgUnits).toHaveLength(0);
  });

  it("includes active runs", () => {
    const s = dispatchOk(initialFleetState(), [], [makeRun({ run_id: "r1", status: "running" })]);
    expect(s.counts.agents).toBe(1);
  });
});

// ─── Join strategy ────────────────────────────────────────────────────────────

describe("fleetReducer — invocation join", () => {
  it("groups run under invocation when invocation_id matches", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "code-review" })],
      [makeRun({ run_id: "r1", status: "running", invocation_id: "inv1" })],
    );
    expect(s.orgUnits).toHaveLength(1);
    expect(s.orgUnits[0].id).toBe("inv1");
    expect(s.orgUnits[0].agents).toHaveLength(1);
    expect(s.orgUnits[0].agents[0].id).toBe("r1");
  });

  it("places unmatched runs in __direct__ group", () => {
    const s = dispatchOk(initialFleetState(), [], [makeRun({ run_id: "r1", status: "running" })]);
    expect(s.orgUnits).toHaveLength(1);
    expect(s.orgUnits[0].id).toBe("__direct__");
    expect(s.orgUnits[0].agents).toHaveLength(1);
  });

  it("invocation without runs still appears", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "review", session_count: 3 })],
      [],
    );
    expect(s.orgUnits).toHaveLength(1);
    expect(s.orgUnits[0].session_count).toBe(3);
  });

  it("mixes: some runs grouped, some direct", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "review" })],
      [
        makeRun({ run_id: "r1", status: "running", invocation_id: "inv1" }),
        makeRun({ run_id: "r2", status: "running" }),
      ],
    );
    const invUnit = s.orgUnits.find((u) => u.id === "inv1");
    const directUnit = s.orgUnits.find((u) => u.id === "__direct__");
    expect(invUnit?.agents).toHaveLength(1);
    expect(directUnit?.agents).toHaveLength(1);
  });
});

// ─── Counts strip ─────────────────────────────────────────────────────────────

describe("fleetReducer — counts strip", () => {
  it("counts orchestrations (non-direct units)", () => {
    const s = dispatchOk(
      initialFleetState(),
      [
        makeInvocation({ id: "i1", status: "running", skill: "a" }),
        makeInvocation({ id: "i2", status: "running", skill: "b" }),
      ],
      [],
    );
    expect(s.counts.orchestrations).toBe(2);
  });

  it("counts direct agents separately — not as orchestrations", () => {
    const s = dispatchOk(initialFleetState(), [], [makeRun({ run_id: "r1", status: "running" })]);
    expect(s.counts.orchestrations).toBe(0);
    expect(s.counts.agents).toBe(1);
  });

  it("counts attention items — gated invocation (active, non-terminal)", () => {
    const nowSec = 2_000_000;
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "i1", status: "gated", skill: "review" })],
      [],
      nowSec,
    );
    expect(s.counts.attention).toBe(1);
  });

  it("stuck agent raises attention count", () => {
    const s = dispatchOk(
      initialFleetState(),
      [],
      [makeRun({ run_id: "r1", status: "running", effective_health: "unresponsive" })],
    );
    expect(s.counts.attention).toBe(1);
  });

  it("dead-health running run is not counted as an active Fleet agent", () => {
    const s = dispatchOk(
      initialFleetState(),
      [],
      [makeRun({ run_id: "r1", status: "running", effective_health: "stale" })],
    );
    expect(s.counts.agents).toBe(0);
    expect(s.orgUnits).toHaveLength(0);
    expect(s.recent[0].id).toBe("r1");
  });
});

// ─── Attention flagging ───────────────────────────────────────────────────────

describe("fleetReducer — attention flagging on org units", () => {
  it("gated invocation is flagged (active, non-terminal)", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "i1", status: "gated", skill: "s" })],
      [],
    );
    expect(s.orgUnits[0].needsAttention).toBe(true);
  });

  it("invocation with stuck child agent is flagged", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "i1", status: "running", skill: "s" })],
      [
        makeRun({
          run_id: "r1",
          status: "running",
          invocation_id: "i1",
          effective_health: "unresponsive",
        }),
      ],
    );
    expect(s.orgUnits[0].needsAttention).toBe(true);
  });

  it("healthy invocation is not flagged", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "i1", status: "running", skill: "s" })],
      [makeRun({ run_id: "r1", status: "running", invocation_id: "i1", started_at: 1_000_000 })],
      1_000_000 + 30,
    );
    expect(s.orgUnits[0].needsAttention).toBe(false);
  });
});

// ─── Sort order ───────────────────────────────────────────────────────────────

describe("fleetReducer — sort order", () => {
  it("units needing attention sort before healthy units", () => {
    const s = dispatchOk(
      initialFleetState(),
      [
        makeInvocation({ id: "i1", status: "running", skill: "healthy" }),
        makeInvocation({ id: "i2", status: "gated", skill: "awaiting-approval" }),
      ],
      [],
    );
    expect(s.orgUnits[0].id).toBe("i2");
    expect(s.orgUnits[1].id).toBe("i1");
  });
});

// ─── terminalRecentRows ───────────────────────────────────────────────────────

describe("terminalRecentRows", () => {
  it("returns every terminal run (no cap) sorted newest first", () => {
    const runs = Array.from({ length: 80 }, (_, i) =>
      makeRun({ run_id: `r${i}`, status: "completed", ended_at: 1_000 + i }),
    );
    runs.push(makeRun({ run_id: "live", status: "running", started_at: 2_000 }));
    const rows = terminalRecentRows(runs);
    expect(rows).toHaveLength(80);
    expect(rows[0].id).toBe("r79");
    expect(rows[79].id).toBe("r0");
    expect(rows.some((r) => r.id === "live")).toBe(false);
  });
});

// ─── Status/verdict unification (design-brief §0/§0b) ────────────────────────
// Fleet must never re-derive lifecycle status on its own — it went through
// deriveDisplayStatus() the same as boardReducer and RunDetail, closing the
// list-vs-detail bug on this view specifically.

describe("fleetReducer — status unification", () => {
  it("terminalRecentRows preserves status_reason_code/summary — it must not drop them", () => {
    const rows = terminalRecentRows([
      makeRun({
        run_id: "r1",
        status: "failed",
        status_reason_code: "session.health.phantom_process_dead",
        status_reason_summary: "phantom_reaped",
      }),
    ]);
    expect(rows[0].status_reason_code).toBe("session.health.phantom_process_dead");
    expect(rows[0].status_reason_summary).toBe("phantom_reaped");
  });

  it("a phantom-reaped row's derived display status is orphaned, not failed", () => {
    const rows = terminalRecentRows([
      makeRun({ run_id: "r1", status: "failed", status_reason_summary: "phantom_reaped" }),
    ]);
    expect(deriveDisplayStatus(rows[0])).toBe("orphaned");
  });

  it("a zombie (stale-locks) reap still derives as a real failure", () => {
    const rows = terminalRecentRows([
      makeRun({
        run_id: "r1",
        status: "failed",
        status_reason_code: "session.zombie.stale_locks",
        status_reason_summary: "phantom_reaped",
      }),
    ]);
    expect(deriveDisplayStatus(rows[0])).toBe("failed");
  });

  it("a 'timeout' alias run is treated as terminal — the local sets this replaced only knew 'timed_out'", () => {
    const s = dispatchOk(initialFleetState(), [], [makeRun({ run_id: "r1", status: "timeout" })]);
    expect(s.orgUnits).toHaveLength(0); // not active
    const rows = terminalRecentRows([makeRun({ run_id: "r1", status: "timeout" })]);
    expect(rows).toHaveLength(1); // shows up in history instead of vanishing
  });
});

// ─── createHistoryPager ───────────────────────────────────────────────────────

describe("createHistoryPager", () => {
  function deferredFetch() {
    const calls: number[] = [];
    let resolve!: (v: { runs: RunSummary[]; has_next: boolean }) => void;
    let reject!: (e: unknown) => void;
    const fetchPage = (page: number) => {
      calls.push(page);
      return new Promise<{ runs: RunSummary[]; has_next: boolean }>((res, rej) => {
        resolve = res;
        reject = rej;
      });
    };
    return { calls, fetchPage, settle: () => resolve, fail: () => reject };
  }

  it("double-fire before the first fetch settles requests each page exactly once", async () => {
    const d = deferredFetch();
    const pager = createHistoryPager(d.fetchPage);

    const first = pager.loadNext();
    const second = pager.loadNext(); // same tick, before the first settles

    expect(d.calls).toEqual([2]); // page 2 fetched once, not twice
    await expect(second).resolves.toBeNull(); // duplicate fire is a no-op

    d.settle()({
      runs: [makeRun({ run_id: "a", status: "completed", ended_at: 1 })],
      has_next: true,
    });
    const page = await first;
    expect(page?.rows.map((r) => r.id)).toEqual(["a"]);
    expect(page?.hasMore).toBe(true);

    void pager.loadNext();
    expect(d.calls).toEqual([2, 3]); // page 3 next — nothing skipped
  });

  it("a failed fetch retries the same page on the next fire", async () => {
    const d = deferredFetch();
    const pager = createHistoryPager(d.fetchPage);

    const first = pager.loadNext();
    d.fail()(new Error("network"));
    await expect(first).resolves.toBeNull();

    void pager.loadNext();
    expect(d.calls).toEqual([2, 2]);
    expect(pager.inFlight()).toBe(true);
  });

  it("reports inFlight only while a fetch is pending", async () => {
    const d = deferredFetch();
    const pager = createHistoryPager(d.fetchPage);
    expect(pager.inFlight()).toBe(false);
    const p = pager.loadNext();
    expect(pager.inFlight()).toBe(true);
    d.settle()({ runs: [], has_next: false });
    await p;
    expect(pager.inFlight()).toBe(false);
  });
});
