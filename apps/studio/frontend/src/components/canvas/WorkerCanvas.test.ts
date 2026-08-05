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
import {
  computeEdgeSourceCompleted,
  panelClearanceShift,
  shouldShowMiniMap,
  shouldShowSidePanel,
} from "./WorkerCanvas";

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

// ─── Side panel earns its width ───────────────────────────────────────────────
// In a read-only embed the panel's empty state is 320px of placeholder text —
// a quarter of the canvas saying "click a step". It appears only once there
// is a selection to show. The editor keeps it always, since add/edit flows
// live in it.

describe("shouldShowSidePanel", () => {
  it("read-only with nothing selected hides the panel", () => {
    expect(shouldShowSidePanel(false, "none")).toBe(false);
  });

  it("read-only with a node selected shows it", () => {
    expect(shouldShowSidePanel(false, "node")).toBe(true);
  });

  it("read-only with an exec result selected shows it", () => {
    expect(shouldShowSidePanel(false, "exec-result")).toBe(true);
  });

  it("read-only with an edge selected shows it", () => {
    expect(shouldShowSidePanel(false, "edge")).toBe(true);
  });

  it("the editor always shows it, selection or not", () => {
    expect(shouldShowSidePanel(true, "none")).toBe(true);
    expect(shouldShowSidePanel(true, "node")).toBe(true);
  });
});

describe("WorkerCanvas.tsx — source contract for the MiniMap", () => {
  const CANVAS_DIR = path.resolve(__dirname);
  const src = fs.readFileSync(path.join(CANVAS_DIR, "WorkerCanvas.tsx"), "utf-8");
  const miniMapTag = src.match(/<MiniMap[\s\S]*?\/>/)?.[0];

  it("declares a compact prop, defaulting to false", () => {
    expect(src).toMatch(/compact\?: boolean/);
    expect(src).toMatch(/compact = false/);
  });

  it("gates the MiniMap through shouldShowMiniMap, not a raw node-count check", () => {
    expect(src).toMatch(/shouldShowMiniMap\(compact, nodes\.length\)/);
  });

  it("docks the non-compact minimap bottom-right at React Flow's default size", () => {
    expect(shouldShowMiniMap(false, 11)).toBe(true);
    expect(miniMapTag).toBeDefined();
    expect(miniMapTag).toMatch(/position="bottom-right"/);
    expect(miniMapTag).not.toMatch(/\b(?:width|height)=/);
    expect(miniMapTag).not.toMatch(/\bstyle=/);
  });
});

// ─── panelClearanceShift — clicked node must clear the overlay panel ─────────
// In read-only embeds the details panel overlays the right 320px of the
// canvas, so a click on a node under that strip summons a panel hiding the
// very node it describes. The shift is computed in pure screen-space math so
// it can be pinned here: the gap these arms close is a pan that never fires
// (shift 0 for a covered node) or fires backwards (negative shift).

describe("panelClearanceShift", () => {
  const CONTAINER = 1200; // panel strip starts at 1200 - 320 = 880

  it("returns 0 for a node fully clear of the panel strip", () => {
    // Node right edge at (100 + 210) * 1 + 0 = 310 — far left of 880.
    expect(panelClearanceShift(100, 210, { x: 0, zoom: 1 }, CONTAINER)).toBe(0);
  });

  it("shifts a covered node left, clear of the strip plus a margin", () => {
    // Node right edge at (700 + 210) * 1 + 100 = 1010 — 130px under the strip.
    const shift = panelClearanceShift(700, 210, { x: 100, zoom: 1 }, CONTAINER);
    expect(shift).toBe(1010 - 880 + 16);
    // Applying the shift puts the node's right edge left of the strip.
    expect(1010 - shift).toBeLessThan(880);
  });

  it("is never negative — a clear node is left where the user put it", () => {
    for (const nodeX of [0, 300, 600, 640]) {
      expect(panelClearanceShift(nodeX, 210, { x: 0, zoom: 1 }, CONTAINER)).toBeGreaterThanOrEqual(
        0,
      );
    }
  });

  it("accounts for zoom: graph coordinates scale before comparing to the strip", () => {
    // At zoom 0.5 the same node's screen right edge is (1600 + 210) * 0.5 = 905.
    const shift = panelClearanceShift(1600, 210, { x: 0, zoom: 0.5 }, CONTAINER);
    expect(shift).toBe(905 - 880 + 16);
    // At zoom 1 it would be 1810 — the un-zoomed math would over-shift.
    expect(shift).toBeLessThan(1810 - 880 + 16);
  });

  it("accounts for the current viewport offset", () => {
    // Same node, viewport panned 400px left: right edge 910 - 400 = 510, clear.
    expect(panelClearanceShift(700, 210, { x: -400, zoom: 1 }, CONTAINER)).toBe(0);
  });

  it("exactly at the strip boundary needs no shift", () => {
    // Right edge exactly 880 — not strictly greater, so no pan.
    expect(panelClearanceShift(670, 210, { x: 0, zoom: 1 }, CONTAINER)).toBe(0);
  });
});
