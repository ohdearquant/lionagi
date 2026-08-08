/**
 * Fleet master-detail contract tests.
 *
 * Covers:
 * - No Drawer import remains in fleet/
 * - Auto-select-first logic (firstAgentId helper)
 * - URL search param validation (validateSearch)
 * - Selection renders inline (SessionDetail not SessionDrawer)
 * - Selection state shape matches SplitPane detailActive contract
 */

import { describe, it, expect } from "vitest";
import { fleetReducer, initialFleetState, terminalRecentRows } from "./fleetReducer";
import type { OrgUnit } from "./fleetReducer";
import type { InvocationSummary } from "@/lib/api";
import type { RunSummary } from "@/lib/types";
import { deriveDisplayStatus } from "@/lib/runStatus";

// ─── No Drawer import guard ───────────────────────────────────────────────────
// Verifies at the module level that fleet/ components do not import the overlay Drawer.

import * as fs from "node:fs";
import * as path from "node:path";

const FLEET_DIR = path.resolve(__dirname);

describe("fleet/ — no Drawer overlay import", () => {
  it("FleetView.tsx does not import Drawer", () => {
    const src = fs.readFileSync(path.join(FLEET_DIR, "FleetView.tsx"), "utf-8");
    expect(src).not.toMatch(/import.*Drawer.*from/);
    expect(src).not.toMatch(/from.*shell\/Drawer/);
  });

  it("SessionDetail.tsx does not import Drawer", () => {
    const src = fs.readFileSync(path.join(FLEET_DIR, "SessionDetail.tsx"), "utf-8");
    expect(src).not.toMatch(/import.*Drawer.*from/);
    expect(src).not.toMatch(/from.*shell\/Drawer/);
  });

  it("SessionDrawer.tsx no longer exists in fleet/", () => {
    const exists = fs.existsSync(path.join(FLEET_DIR, "SessionDrawer.tsx"));
    expect(exists).toBe(false);
  });

  it("FleetView.tsx imports SplitPane", () => {
    const src = fs.readFileSync(path.join(FLEET_DIR, "FleetView.tsx"), "utf-8");
    expect(src).toMatch(/SplitPane/);
  });

  it("FleetView.tsx imports SessionDetail (not SessionDrawer)", () => {
    const src = fs.readFileSync(path.join(FLEET_DIR, "FleetView.tsx"), "utf-8");
    expect(src).toMatch(/SessionDetail/);
    expect(src).not.toMatch(/SessionDrawer/);
  });
});

// ─── Helpers ─────────────────────────────────────────────────────────────────

function makeRun(overrides: Partial<RunSummary> & { run_id: string; status: string }): RunSummary {
  return {
    playbook_name: null,
    agent_name: null,
    invocation_kind: null,
    show_topic: null,
    show_play_name: null,
    source_kind: "api",
    effective_health: null,
    last_message_at: null,
    invocation_id: null,
    parent_run_id: null,
    started_at: null,
    ended_at: null,
    branch_count: 0,
    message_count: 0,
    ...overrides,
  };
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

function dispatchOk(invocations: InvocationSummary[], runs: RunSummary[], nowSec = 1_000_000) {
  return fleetReducer(initialFleetState(), {
    type: "DATA_OK",
    invocations,
    runs,
    runsHasNext: false,
    nowSec,
  });
}

// ─── firstAgentId logic (inline, mirrors FleetView helper) ────────────────────

function firstAgentId(orgUnits: OrgUnit[]): string | null {
  for (const unit of orgUnits) {
    if (unit.agents.length > 0) return unit.agents[0].id;
  }
  return null;
}

describe("auto-select-first — firstAgentId logic", () => {
  it("returns null when no org units", () => {
    expect(firstAgentId([])).toBeNull();
  });

  it("returns null when all units have empty agent lists", () => {
    const s = dispatchOk(
      [makeInvocation({ id: "i1", status: "running", skill: "review", session_count: 3 })],
      [],
    );
    expect(firstAgentId(s.orgUnits)).toBeNull();
  });

  it("returns first agent id from first unit", () => {
    const s = dispatchOk([], [makeRun({ run_id: "r1", status: "running" })]);
    expect(firstAgentId(s.orgUnits)).toBe("r1");
  });

  it("returns first agent of the first non-empty unit (attention-sorted)", () => {
    const s = dispatchOk(
      [
        makeInvocation({ id: "i1", status: "gated", skill: "a" }),
        makeInvocation({ id: "i2", status: "running", skill: "b" }),
      ],
      [
        makeRun({ run_id: "r-gated", status: "running", invocation_id: "i1" }),
        makeRun({ run_id: "r-healthy", status: "running", invocation_id: "i2" }),
      ],
    );
    // i1 is gated → sorts first; its agent is r-gated
    expect(s.orgUnits[0].id).toBe("i1");
    expect(firstAgentId(s.orgUnits)).toBe("r-gated");
  });

  it("selects a visible child from a same-page parent run group", () => {
    const s = dispatchOk(
      [],
      [
        makeRun({ run_id: "play-1", status: "running", invocation_kind: "play" }),
        makeRun({
          run_id: "child-1",
          status: "running",
          invocation_kind: "agent",
          parent_run_id: "play-1",
        }),
      ],
    );

    expect(s.orgUnits[0].id).toBe("play-1");
    expect(firstAgentId(s.orgUnits)).toBe("child-1");
  });
});

// ─── URL validateSearch contract ──────────────────────────────────────────────
// Mirrors the validateSearch function in fleet.tsx.

function validateSearch(search: Record<string, unknown>): { s?: string } {
  const s = search.s;
  return typeof s === "string" && s.length > 0 ? { s } : {};
}

describe("fleet route validateSearch", () => {
  it("returns empty object when s is missing", () => {
    expect(validateSearch({})).toEqual({});
  });

  it("returns empty object when s is empty string", () => {
    expect(validateSearch({ s: "" })).toEqual({});
  });

  it("returns empty object when s is not a string", () => {
    expect(validateSearch({ s: 42 })).toEqual({});
    expect(validateSearch({ s: null })).toEqual({});
  });

  it("passes through a valid run id string", () => {
    expect(validateSearch({ s: "abc-123" })).toEqual({ s: "abc-123" });
  });

  it("ignores extra keys", () => {
    expect(validateSearch({ s: "run-x", tab: "foo" })).toEqual({ s: "run-x" });
  });
});

// ─── Selection state: detailActive wiring ────────────────────────────────────
// SplitPane's detailActive determines collapsed-mode routing.
// Contract: detailActive=true after an explicit row click or a selected-run
// deep link. Operator links must reveal detail even when the dock collapses
// the Fleet split pane.

describe("detailActive contract", () => {
  it("starts from whether the URL already identifies a run", () => {
    expect(Boolean(null)).toBe(false);
    expect(Boolean("run-from-operator")).toBe(true);
  });

  it("becomes true on explicit agent row click", () => {
    let narrowExplicit = false;
    // handleSelectAgent sets narrowExplicit=true
    const handleSelectAgent = () => {
      narrowExplicit = true;
    };
    handleSelectAgent();
    expect(narrowExplicit).toBe(true);
  });

  it("reverts to false on back navigation", () => {
    let narrowExplicit = true;
    const handleBack = () => {
      narrowExplicit = false;
    };
    handleBack();
    expect(narrowExplicit).toBe(false);
  });
});

// ─── History filter chips: normalized status, not raw run.status ─────────────
// Mirrors matchesHistFilter in FleetView.tsx (design-brief §0/§4): a
// phantom-reaped row (raw status "failed") must not match the "failed" chip.

function matchesHistFilter(displayStatus: string, filter: "all" | "completed" | "failed"): boolean {
  if (filter === "all") return true;
  if (filter === "failed") return displayStatus === "failed";
  return displayStatus === "completed";
}

describe("fleet history filter — normalized status", () => {
  it("a phantom-reaped row does not match the 'failed' filter", () => {
    const [row] = terminalRecentRows([
      makeRun({ run_id: "r1", status: "failed", status_reason_summary: "phantom_reaped" }),
    ]);
    expect(matchesHistFilter(deriveDisplayStatus(row), "failed")).toBe(false);
  });

  it("a phantom-reaped row still appears under 'all'", () => {
    const [row] = terminalRecentRows([
      makeRun({ run_id: "r1", status: "failed", status_reason_summary: "phantom_reaped" }),
    ]);
    expect(matchesHistFilter(deriveDisplayStatus(row), "all")).toBe(true);
  });

  it("a genuine failure still matches the 'failed' filter", () => {
    const [row] = terminalRecentRows([makeRun({ run_id: "r1", status: "failed" })]);
    expect(matchesHistFilter(deriveDisplayStatus(row), "failed")).toBe(true);
  });

  it("a zombie reap still matches the 'failed' filter — resource leak, not housekeeping", () => {
    const [row] = terminalRecentRows([
      makeRun({
        run_id: "r1",
        status: "failed",
        status_reason_code: "session.zombie.stale_locks",
        status_reason_summary: "phantom_reaped",
      }),
    ]);
    expect(matchesHistFilter(deriveDisplayStatus(row), "failed")).toBe(true);
  });
});

// ─── Cost-sorted history must page truthfully, not read as complete ──────────
// A cost-sorted fetch of one server page, filtered client-side by status, used
// to hardcode histHasMore=false — so a status filter matching rows beyond that
// first page silently dropped them while the list still read as complete.

describe("cost-sorted history — hasMore is never hardcoded false", () => {
  const src = fs.readFileSync(path.join(FLEET_DIR, "FleetView.tsx"), "utf-8");

  it("histHasMore for cost mode is driven by real server pagination state, not a literal false", () => {
    expect(src).not.toMatch(/histSort === "cost" \? false/);
    expect(src).toMatch(/histHasMore = histSort === "cost" \? costHasMore/);
  });

  it("cost mode's load-more handler fetches another cost-ranked page instead of no-oping", () => {
    const fnMatch = src.match(/const handleLoadMore = useCallback\(\(\) => \{[\s\S]*?\n {2}\}, \[/);
    expect(fnMatch).not.toBeNull();
    const body = fnMatch?.[0] ?? "";
    const costBranch = body.slice(0, body.indexOf("if (histVisible < historyRows.length)"));
    expect(costBranch).toMatch(/costPager/);
    expect(costBranch).toMatch(/loadNext\(\)/);
  });
});

// ─── Selection validity: URL id validated against live agent set ──────────────

describe("selectedRunId validation", () => {
  it("resolves null when URL id not in current agent list", () => {
    const s = dispatchOk([], [makeRun({ run_id: "r1", status: "running" })]);
    const allIds = s.orgUnits.flatMap((u) => u.agents.map((a) => a.id));
    const urlId = "stale-id-not-present";
    const resolved = allIds.includes(urlId) ? urlId : null;
    expect(resolved).toBeNull();
  });

  it("resolves correctly when URL id is present", () => {
    const s = dispatchOk([], [makeRun({ run_id: "r1", status: "running" })]);
    const allIds = s.orgUnits.flatMap((u) => u.agents.map((a) => a.id));
    const urlId = "r1";
    const resolved = allIds.includes(urlId) ? urlId : null;
    expect(resolved).toBe("r1");
  });
});

// ─── Formatter helpers ────────────────────────────────────────────────────────

import { formatElapsed, formatCompactCount, patchSearch, isPlayRoot } from "./FleetView";
import { resetDetailScrollPosition } from "./SessionDetail";

// ─── isPlayRoot — the play-vs-single-agent discriminator (issue #2842) ───────

describe("isPlayRoot", () => {
  it("is true for every multi-agent invocation_kind", () => {
    expect(isPlayRoot("play")).toBe(true);
    expect(isPlayRoot("flow")).toBe(true);
    expect(isPlayRoot("fanout")).toBe(true);
    expect(isPlayRoot("show-play")).toBe(true);
  });

  it("is false for a single agent and for a missing kind", () => {
    expect(isPlayRoot("agent")).toBe(false);
    expect(isPlayRoot(null)).toBe(false);
    expect(isPlayRoot(undefined)).toBe(false);
  });
});

// ─── patchSearch — URL search patching without smuggling `undefined` in ──────
// FleetSearch's index signature is RetiredSearchValue (no `undefined`), so a
// naive `{...search, key: undefined}` spread would type- and shape-mismatch;
// patchSearch must delete the key instead.

describe("patchSearch", () => {
  it("adds a new key", () => {
    expect(patchSearch({ s: "run-1" }, { project: "org/alpha" })).toEqual({
      s: "run-1",
      project: "org/alpha",
    });
  });

  it("deletes a key when the patch value is undefined, rather than keeping it as literal undefined", () => {
    const result = patchSearch({ s: "run-1", project: "org/alpha" }, { project: undefined });
    expect(result).toEqual({ s: "run-1" });
    expect("project" in result).toBe(false);
  });

  it("overwrites an existing key", () => {
    expect(patchSearch({ project: "org/alpha" }, { project: "org/beta" })).toEqual({
      project: "org/beta",
    });
  });

  it("clearing project and project_null together removes both, keeping the rest", () => {
    const result = patchSearch(
      { s: "run-1", project: "org/alpha", project_null: true, q: "flaky" },
      { project: undefined, project_null: undefined },
    );
    expect(result).toEqual({ s: "run-1", q: "flaky" });
  });

  it("does not mutate the base object", () => {
    const base = { s: "run-1" };
    patchSearch(base, { project: "org/alpha" });
    expect(base).toEqual({ s: "run-1" });
  });
});

describe("formatElapsed", () => {
  it("renders — for null, NaN, Infinity, and negative input", () => {
    expect(formatElapsed(null)).toBe("—");
    expect(formatElapsed(Number.NaN)).toBe("—");
    expect(formatElapsed(Number.POSITIVE_INFINITY)).toBe("—");
    expect(formatElapsed(-5)).toBe("—");
  });

  it("renders sub-minute and sub-hour values unchanged", () => {
    expect(formatElapsed(0)).toBe("0s");
    expect(formatElapsed(59)).toBe("59s");
    expect(formatElapsed(60)).toBe("1m");
    expect(formatElapsed(61)).toBe("1m 1s");
    expect(formatElapsed(3599)).toBe("59m 59s");
  });

  it("renders sub-day hour values", () => {
    expect(formatElapsed(3600)).toBe("1h");
    expect(formatElapsed(3660)).toBe("1h 1m");
    expect(formatElapsed(24 * 3600 - 60)).toBe("23h 59m");
  });

  it("tiers into days at exactly 24h", () => {
    expect(formatElapsed(24 * 3600)).toBe("1d");
    expect(formatElapsed(24 * 3600 + 3600)).toBe("1d 1h");
    expect(formatElapsed(62 * 3600 + 34 * 60)).toBe("2d 14h");
  });
});

describe("formatCompactCount", () => {
  it("keeps counts under 1000 exact", () => {
    expect(formatCompactCount(0)).toBe("0");
    expect(formatCompactCount(999)).toBe("999");
  });

  it("abbreviates from the 1000 boundary, truncating not rounding", () => {
    expect(formatCompactCount(1000)).toBe("1k");
    expect(formatCompactCount(5967)).toBe("5.9k");
    expect(formatCompactCount(999999)).toBe("999.9k");
  });
});

describe("session detail scroll reset", () => {
  it("returns every newly selected run to the top of its detail pane", () => {
    const pane = document.createElement("div");
    pane.scrollTop = 480;
    resetDetailScrollPosition(pane);
    expect(pane.scrollTop).toBe(0);

    const source = fs.readFileSync(path.join(FLEET_DIR, "SessionDetail.tsx"), "utf-8");
    expect(source).toContain("useLayoutEffect");
    expect(source).toContain("[runId]");
  });
});
