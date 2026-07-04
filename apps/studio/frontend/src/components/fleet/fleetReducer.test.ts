import { describe, it, expect } from "vitest";
import { fleetReducer, initialFleetState } from "./fleetReducer";
import type { FleetState } from "./fleetReducer";
import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";

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
  return fleetReducer(state, { type: "DATA_OK", invocations, runs, nowSec });
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
    const nowSec = 2_000_000;
    const s = dispatchOk(
      initialFleetState(),
      [],
      [makeRun({ run_id: "r1", status: "running", started_at: nowSec - 4000 })],
      nowSec,
    );
    expect(s.counts.attention).toBe(1);
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
    const nowSec = 2_000_000;
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "i1", status: "running", skill: "s" })],
      [
        makeRun({
          run_id: "r1",
          status: "running",
          invocation_id: "i1",
          started_at: nowSec - 4000,
        }),
      ],
      nowSec,
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
