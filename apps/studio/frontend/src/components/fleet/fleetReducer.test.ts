import { describe, it, expect } from "vitest";
import {
  fleetReducer,
  initialFleetState,
  terminalRecentRows,
  terminalRecentRowsServerOrder,
  createHistoryPager,
} from "./fleetReducer";
import type { FleetState } from "./fleetReducer";
import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";
import { deriveDisplayStatus } from "@/lib/runStatus";
import { resolveRunLabel } from "@/lib/runLabel";

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
  scope?: { project?: string; projectNull?: boolean; search?: string },
  runsHasNext = false,
): FleetState {
  return fleetReducer(state, {
    type: "DATA_OK",
    invocations,
    runs,
    runsHasNext,
    nowSec,
    ...scope,
  });
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

  it("keeps sessions distinct when compatibility run_id values collide", () => {
    const s = dispatchOk(
      initialFleetState(),
      [],
      [
        makeRun({ id: "session-1", run_id: "shared-run", status: "running" }),
        makeRun({ id: "session-2", run_id: "shared-run", status: "running" }),
      ],
    );

    expect(s.orgUnits[0].agents.map((agent) => agent.id)).toEqual(["session-1", "session-2"]);
  });

  it("an agent row's name matches the shared resolver, never a raw playbook/agent fallback", () => {
    const run = makeRun({
      run_id: "r1",
      status: "running",
      playbook_name: "pr-merge-review",
      agent_name: "implementer",
    });
    const s = dispatchOk(initialFleetState(), [], [run]);
    expect(s.orgUnits[0].agents[0].name).toBe(resolveRunLabel(run));
  });

  it("carries invocation_kind onto the agent row so a play root is distinguishable from a single agent", () => {
    const s = dispatchOk(
      initialFleetState(),
      [],
      [
        makeRun({ run_id: "r1", status: "running", invocation_kind: "play" }),
        makeRun({ run_id: "r2", status: "running", invocation_kind: "agent" }),
      ],
    );
    const agents = s.orgUnits[0].agents;
    expect(agents.find((a) => a.id === "r1")?.invocationKind).toBe("play");
    expect(agents.find((a) => a.id === "r2")?.invocationKind).toBe("agent");
  });

  it("invocation without runs still appears when no scope is active", () => {
    // Names the case that would have passed either way: no project/search
    // filter is in play here, so this test exercises the same code path
    // before and after the scoping fix below and cannot discriminate them.
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

// ─── Invocation groups respect the same scope as the runs projection ─────────
// Reproduces: an active invocation with no child in the scoped runs page
// previously still rendered a group (global session_count header + a "no
// agents" row), under both a project scope and a search filter — because
// buildOrgUnits created a unit for every active invocation regardless of
// whether the (separately-scoped) runs page had anything under it.

describe("fleetReducer — invocation groups respect runs scope", () => {
  it("drops a non-matching invocation under a project scope", () => {
    // inv1's own project is "beta"; the runs page is scoped to "alpha" and
    // (correctly, since scoping is server-side on runs) contains no child of
    // inv1. Before the fix this rendered anyway: FAILS on current head.
    const s = dispatchOk(
      initialFleetState(),
      [
        makeInvocation({
          id: "inv1",
          status: "running",
          skill: "review",
          session_count: 5,
          project: "beta",
        }),
      ],
      [],
      1_000_000,
      { project: "alpha" },
    );
    expect(s.orgUnits.find((u) => u.id === "inv1")).toBeUndefined();
  });

  it("drops a non-matching invocation under a search filter", () => {
    // Same shape, but the active scope is a name/agent search instead of a
    // project. inv1 has no child in the scoped runs page either way — the
    // group must not render regardless of which filter produced the scope.
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "review", session_count: 5 })],
      [],
      1_000_000,
      { search: "cleanup" },
    );
    expect(s.orgUnits.find((u) => u.id === "inv1")).toBeUndefined();
  });

  it("still shows a matching invocation, with its count, under a project scope", () => {
    // Positive direction: a suppress-everything implementation (e.g. "if
    // scoped, always return []") fails this test.
    const s = dispatchOk(
      initialFleetState(),
      [
        makeInvocation({
          id: "inv1",
          status: "running",
          skill: "review",
          session_count: 2,
          project: "alpha",
        }),
      ],
      [makeRun({ run_id: "r1", status: "running", invocation_id: "inv1", project: "alpha" })],
      1_000_000,
      { project: "alpha" },
    );
    const unit = s.orgUnits.find((u) => u.id === "inv1");
    expect(unit).toBeDefined();
    expect(unit?.agents).toHaveLength(1);
    // The scoped count, not the invocation's global session_count of 2: a
    // filtered child list under a total that counts unfiltered children states
    // a number belonging to a different question.
    expect(unit?.session_count).toBe(1);
  });

  it("still shows a matching invocation under a search filter", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "review", session_count: 1 })],
      [makeRun({ run_id: "r1", status: "running", invocation_id: "inv1" })],
      1_000_000,
      { search: "cleanup" },
    );
    const unit = s.orgUnits.find((u) => u.id === "inv1");
    expect(unit).toBeDefined();
    expect(unit?.agents).toHaveLength(1);
  });

  it("keeps a childless invocation when the runs page is not the whole scoped set", () => {
    // The runs page is bounded. With more scoped rows behind it, "no child
    // here" is not "no child in scope": the matching child can be on a page
    // nobody asked for. Suppressing an empty heading is cosmetic; hiding a
    // running orchestration from the view that is supposed to include it is
    // not, so the suppression only applies where the page is the whole set.
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "review", session_count: 5 })],
      [],
      1_000_000,
      { project: "alpha" },
      true,
    );
    expect(s.orgUnits.find((u) => u.id === "inv1")).toBeDefined();
  });

  it("reports the scoped child count, not the global one, on a surviving group", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "review", session_count: 5 })],
      [makeRun({ run_id: "r1", status: "running", invocation_id: "inv1", project: "alpha" })],
      1_000_000,
      { project: "alpha" },
    );
    expect(s.orgUnits.find((u) => u.id === "inv1")?.session_count).toBe(1);
  });

  it("reports the invocation's own count when nothing is scoped", () => {
    // Control: the scoped count must not become the count everywhere. With no
    // filter, the group's total is the invocation's own, children beyond this
    // page included.
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "inv1", status: "running", skill: "review", session_count: 5 })],
      [makeRun({ run_id: "r1", status: "running", invocation_id: "inv1" })],
    );
    expect(s.orgUnits.find((u) => u.id === "inv1")?.session_count).toBe(5);
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

  it("counts a running play that carries no invocation_id — it forms no group", () => {
    // The regression this guards: plays, fanouts and flows never populate
    // invocation_id, so they group under nothing and the strip read zero while
    // the run was visibly listed as active underneath it.
    const s = dispatchOk(
      initialFleetState(),
      [],
      [makeRun({ run_id: "p1", status: "running", invocation_kind: "play" })],
    );
    expect(s.counts.orchestrations).toBe(1);
  });

  it.each(["play", "fanout", "flow"])("counts an ungrouped %s run", (kind) => {
    const s = dispatchOk(
      initialFleetState(),
      [],
      [makeRun({ run_id: "x1", status: "running", invocation_kind: kind })],
    );
    expect(s.counts.orchestrations).toBe(1);
  });

  it("does not double count a play that DID join an invocation group", () => {
    const s = dispatchOk(
      initialFleetState(),
      [makeInvocation({ id: "i1", status: "running", skill: "a" })],
      [
        makeRun({
          run_id: "p1",
          status: "running",
          invocation_kind: "play",
          invocation_id: "i1",
        }),
      ],
    );
    // One orchestration, evidenced twice. The group wins; the run is not added.
    expect(s.counts.orchestrations).toBe(1);
  });

  it("does not count a terminal play run", () => {
    const s = dispatchOk(
      initialFleetState(),
      [],
      [makeRun({ run_id: "p1", status: "completed", invocation_kind: "play" })],
    );
    expect(s.counts.orchestrations).toBe(0);
  });

  it("counts an agent-kind run as an agent, never as an orchestration", () => {
    const s = dispatchOk(
      initialFleetState(),
      [],
      [makeRun({ run_id: "a1", status: "running", invocation_kind: "agent" })],
    );
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

  it("preserves a null total_cost_usd as unreported, not a coerced 0", () => {
    const rows = terminalRecentRows([
      makeRun({ run_id: "r1", status: "completed", total_cost_usd: null }),
    ]);
    expect(rows[0].totalCostUsd).toBeNull();
  });

  it("preserves a genuine zero total_cost_usd distinctly from unreported", () => {
    const rows = terminalRecentRows([
      makeRun({ run_id: "r1", status: "completed", total_cost_usd: 0 }),
    ]);
    expect(rows[0].totalCostUsd).toBe(0);
  });

  it("carries a reported cost through", () => {
    const rows = terminalRecentRows([
      makeRun({ run_id: "r1", status: "completed", total_cost_usd: 4.5 }),
    ]);
    expect(rows[0].totalCostUsd).toBe(4.5);
  });
});

describe("terminalRecentRowsServerOrder", () => {
  it("preserves the input order instead of re-sorting by ended_at", () => {
    // Deliberately out of ended_at order — as /api/runs/?sort=cost would
    // return: highest cost first, unrelated to recency.
    const runs = [
      makeRun({ run_id: "cheap", status: "completed", ended_at: 5_000, total_cost_usd: 1 }),
      makeRun({ run_id: "pricey", status: "completed", ended_at: 1_000, total_cost_usd: 99 }),
      makeRun({ run_id: "free", status: "completed", ended_at: 3_000, total_cost_usd: 0 }),
    ];
    const rows = terminalRecentRowsServerOrder(runs);
    expect(rows.map((r) => r.id)).toEqual(["cheap", "pricey", "free"]);
  });

  it("still excludes active runs", () => {
    const rows = terminalRecentRowsServerOrder([
      makeRun({ run_id: "live", status: "running", started_at: 1 }),
      makeRun({ run_id: "done", status: "completed", ended_at: 1 }),
    ]);
    expect(rows.map((r) => r.id)).toEqual(["done"]);
  });

  it("a recent row's name matches the shared resolver, never a raw playbook/agent fallback", () => {
    const run = makeRun({
      run_id: "r1",
      status: "completed",
      ended_at: 1_000,
      playbook_name: "pr-merge-review",
      agent_name: "implementer",
    });
    const rows = terminalRecentRows([run]);
    expect(rows[0].name).toBe(resolveRunLabel(run));
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

  it("a custom mapRows (cost order) is applied to later pages instead of the recency default — the 'Highest cost' history sort must not have its order undone once paging kicks in", async () => {
    const d = deferredFetch();
    const pager = createHistoryPager(d.fetchPage, 2, terminalRecentRowsServerOrder);
    const p = pager.loadNext();
    // Deliberately out of ended_at order, as /api/runs/?sort=cost returns it —
    // terminalRecentRows (the default mapRows) would re-sort this by recency.
    d.settle()({
      runs: [
        makeRun({ run_id: "pricey", status: "completed", ended_at: 1, total_cost_usd: 99 }),
        makeRun({ run_id: "cheap", status: "completed", ended_at: 5, total_cost_usd: 1 }),
      ],
      has_next: true,
    });
    const page = await p;
    expect(page?.rows.map((r) => r.id)).toEqual(["pricey", "cheap"]);
  });

  it("cost sort + status filter: a matching row beyond the first cost-ranked page surfaces on the next page, and hasMore stays true until the server says otherwise — it never reads as a complete, silently-truncated list", async () => {
    const d = deferredFetch();
    const pager = createHistoryPager(d.fetchPage, 2, terminalRecentRowsServerOrder);

    // First cost-ranked page: no "failed" rows at all.
    const first = pager.loadNext();
    d.settle()({
      runs: [makeRun({ run_id: "expensive-ok", status: "completed", total_cost_usd: 500 })],
      has_next: true,
    });
    const page1 = await first;
    expect(page1?.hasMore).toBe(true);
    const filteredPage1 = (page1?.rows ?? []).filter((r) => deriveDisplayStatus(r) === "failed");
    expect(filteredPage1).toHaveLength(0); // none yet — but hasMore says keep going, not "done"

    // Second cost-ranked page: the matching row was here all along.
    const second = pager.loadNext();
    d.settle()({
      runs: [makeRun({ run_id: "cheap-failed", status: "failed", total_cost_usd: 1 })],
      has_next: false,
    });
    const page2 = await second;
    expect(page2?.hasMore).toBe(false);
    const filteredPage2 = (page2?.rows ?? []).filter((r) => deriveDisplayStatus(r) === "failed");
    expect(filteredPage2.map((r) => r.id)).toEqual(["cheap-failed"]);
  });
});
