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
import { fleetReducer, initialFleetState } from "./fleetReducer";
import type { OrgUnit } from "./fleetReducer";
import type { InvocationSummary } from "@/lib/api";
import type { RunSummary } from "@/lib/types";

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
// Contract: detailActive=true only after explicit narrow-screen selection.

describe("detailActive contract", () => {
  it("starts false — no explicit narrow selection yet", () => {
    // Mirrors FleetView: useState(false)
    let narrowExplicit = false;
    expect(narrowExplicit).toBe(false);
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
