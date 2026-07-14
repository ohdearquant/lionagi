/**
 * computeEdgeSourceCompleted — edge completion fallback.
 *
 * RunDetail always passes a truthy nodeStatuses object once a planned graph
 * exists — `{}` for legacy runs, or a partial map when a run's signals
 * don't correlate to every authored id. An edge whose source node has no
 * entry in that map must fall back to the legacy execSteps-derived
 * completedMap, not be treated as pending just because *some* nodeStatuses
 * object was supplied.
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { computeEdgeSourceCompleted, shouldShowMiniMap } from "./WorkerCanvas";

describe("computeEdgeSourceCompleted", () => {
  it("uses the legacy completedMap when nodeStatuses is undefined", () => {
    const completedMap = new Map([["a", { step: "a", status: "completed" }]]);
    expect(computeEdgeSourceCompleted("a", undefined, completedMap)).toBe(true);
    expect(computeEdgeSourceCompleted("b", undefined, completedMap)).toBe(false);
  });

  it("legacy run: nodeStatuses is {} (no live signal correlation) — falls back to completedMap", () => {
    const completedMap = new Map([["a", { step: "a", status: "completed" }]]);
    expect(computeEdgeSourceCompleted("a", {}, completedMap)).toBe(true);
    expect(computeEdgeSourceCompleted("b", {}, completedMap)).toBe(false);
  });

  it("mixed run: node covered by nodeStatuses uses the live value, even when not completed", () => {
    const completedMap = new Map([["a", { step: "a", status: "completed" }]]);
    // "a" has a live signal saying it's still running — that must win over
    // whatever the legacy execSteps map says.
    expect(computeEdgeSourceCompleted("a", { a: "running" }, completedMap)).toBe(false);
    expect(computeEdgeSourceCompleted("a", { a: "completed" }, completedMap)).toBe(true);
  });

  it("mixed run: node NOT covered by nodeStatuses falls back to completedMap", () => {
    const completedMap = new Map([["b", { step: "b", status: "completed" }]]);
    // "a" has live coverage (irrelevant here), "b" has none — must use the
    // legacy fallback for "b" rather than defaulting to not-completed.
    expect(computeEdgeSourceCompleted("b", { a: "running" }, completedMap)).toBe(true);
  });

  it("node with no coverage anywhere and no legacy record is not completed", () => {
    expect(computeEdgeSourceCompleted("z", {}, new Map())).toBe(false);
  });
});

// ─── MiniMap suppressed in compact embeds ─────────────────────────────────────
// RunDetail's run-dag panel is a fixed 280px-tall container — at that size a
// MiniMap reads as a floating cluster of gray micro-nodes rather than a
// useful overview, so `compact` embeds must never show it, no matter how
// many nodes the graph has. Non-compact usage (the full-page graph editor)
// keeps the existing >10-nodes threshold.

describe("shouldShowMiniMap", () => {
  it("compact embed never shows the minimap, even with many nodes", () => {
    expect(shouldShowMiniMap(true, 50)).toBe(false);
  });

  it("compact embed hides the minimap when under the node threshold too", () => {
    expect(shouldShowMiniMap(true, 3)).toBe(false);
  });

  it("non-compact usage shows the minimap once nodes exceed the threshold", () => {
    expect(shouldShowMiniMap(false, 11)).toBe(true);
  });

  it("non-compact usage hides the minimap at or under the threshold", () => {
    expect(shouldShowMiniMap(false, 10)).toBe(false);
  });
});

describe("WorkerCanvas.tsx — source contract for the compact MiniMap fix", () => {
  const CANVAS_DIR = path.resolve(__dirname);
  const src = fs.readFileSync(path.join(CANVAS_DIR, "WorkerCanvas.tsx"), "utf-8");

  it("declares a compact prop, defaulting to false", () => {
    expect(src).toMatch(/compact\?: boolean/);
    expect(src).toMatch(/compact = false/);
  });

  it("gates the MiniMap through shouldShowMiniMap, not a raw node-count check", () => {
    expect(src).toMatch(/shouldShowMiniMap\(compact, nodes\.length\)/);
  });

  it("docks the non-compact minimap bottom-right and keeps React Flow's default size", () => {
    expect(shouldShowMiniMap(false, 11)).toBe(true);
    expect(src).toMatch(/position="bottom-right"/);
    expect(src).not.toMatch(/style=\{\{ width: \d+, height: \d+ \}\}/);
  });
});
