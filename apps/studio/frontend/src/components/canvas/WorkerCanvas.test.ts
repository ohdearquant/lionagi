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
  fitZoomFor,
  FIT_ZOOM_FLOOR,
  MIN_INTERACTIVE_ZOOM,
  computeEffectiveNodeStatuses,
  computeFollowCenter,
  panelClearanceShift,
  shouldShowMiniMap,
  shouldShowSidePanel,
} from "./WorkerCanvas";
import type { NodeExecStatus } from "./StepNode";

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

// ─── fitZoomFor / FIT_ZOOM_FLOOR — the readability floor ─────────────────────
// StepNode's smallest text (label, role, assignment, stats) renders at
// --t-xs (11px). Below a 7px screen size that stops being legible:
// 7 / 11 = 0.636, rounded up to 0.65 for a small margin. fitZoomFor mirrors
// ReactFlow's own fit-to-container math so layout fixtures (useLayout.test.ts)
// can assert "this graph's raw fit zoom clears/misses the floor" without
// mounting ReactFlow; the floor itself is enforced by wiring FIT_ZOOM_FLOOR
// into <ReactFlow minZoom> (below), which clamps regardless of what the raw
// number says — a graph whose natural fit falls below it overflows the
// container and pans/scrolls instead of shrinking further.

describe("FIT_ZOOM_FLOOR", () => {
  it("is derived from --t-xs (11px) at a 7px minimum legible screen size", () => {
    expect(FIT_ZOOM_FLOOR).toBeCloseTo(0.65, 5);
    expect(FIT_ZOOM_FLOOR).toBeGreaterThan(7 / 11);
  });
});

describe("fitZoomFor", () => {
  it("fits a small graph in a large viewport at maxZoom, not blown past it", () => {
    expect(fitZoomFor(400, 200, 1200, 800, 0.15, 1)).toBe(1);
  });

  it("shrinks a graph wider than the viewport can show at 1x", () => {
    const zoom = fitZoomFor(3000, 300, 1280, 560, 0.15, 1);
    expect(zoom).toBeLessThan(1);
    expect(zoom).toBeGreaterThan(0);
  });

  it("is width-bound when the graph is wide and short", () => {
    // minZoom=0 (raw, unclamped) to isolate the axis-bound arithmetic itself
    // from the floor clamp — this case falls under the floor (see the
    // "falls below" test below), which the default minZoom would mask.
    const zoom = fitZoomFor(3000, 100, 1280, 800, 0.15, 1, 0);
    const expected = 1280 / (3000 * 1.15);
    expect(zoom).toBeCloseTo(expected, 5);
  });

  it("is height-bound when the graph is tall and narrow", () => {
    const zoom = fitZoomFor(200, 2000, 1280, 560, 0.15, 1, 0);
    const expected = 560 / (2000 * 1.15);
    expect(zoom).toBeCloseTo(expected, 5);
  });

  it("raw (unclamped) arithmetic falls below the readability floor for a graph too large for the panel", () => {
    // The deep-chain fan-in fixture (useLayout.test.ts) lands here — this is
    // exactly the case the minZoom clamp exists for. minZoom=0 isolates the
    // raw arithmetic from the default clamp asserted in the next test.
    const zoom = fitZoomFor(1968, 1127, 1280, 560, 0.15, 1, 0);
    expect(zoom).toBeLessThan(FIT_ZOOM_FLOOR);
  });

  it("clamps to the readability floor by default for that same too-large graph", () => {
    // Same inputs as above, default minZoom (FIT_ZOOM_FLOOR) — matches what
    // WorkerCanvas's <ReactFlow minZoom={FIT_ZOOM_FLOOR}> actually renders.
    const zoom = fitZoomFor(1968, 1127, 1280, 560, 0.15, 1);
    expect(zoom).toBe(FIT_ZOOM_FLOOR);
  });

  it("clears the floor for a small, compact graph", () => {
    const zoom = fitZoomFor(600, 200, 1280, 560, 0.15, 1);
    expect(zoom).toBeGreaterThanOrEqual(FIT_ZOOM_FLOOR);
  });
});

describe("WorkerCanvas.tsx — source contract for the readability floor clamp", () => {
  const CANVAS_DIR = path.resolve(__dirname);
  const src = fs.readFileSync(path.join(CANVAS_DIR, "WorkerCanvas.tsx"), "utf-8");

  it("keeps the readability floor OFF the ReactFlow root, so zoom-out still reaches a whole graph", () => {
    // The root minZoom bounds every zoom gesture — wheel, pinch, the Controls
    // zoom-out button — not just the fit. Setting it to the readability floor
    // makes a graph whose natural fit is below that floor permanently
    // unviewable in a compact embed, which has no minimap either. The floor
    // belongs to the fit; the root gets a much lower interactive bound.
    expect(src).not.toMatch(/minZoom=\{FIT_ZOOM_FLOOR\}/);
    expect(src).toMatch(/minZoom=\{MIN_INTERACTIVE_ZOOM\}/);
    expect(MIN_INTERACTIVE_ZOOM).toBeLessThan(FIT_ZOOM_FLOOR);
  });

  it("still sets the readability floor in fitViewOptions for the initial fit", () => {
    const fitViewOptions = src.match(/fitViewOptions=\{\{[\s\S]*?\}\}/)?.[0];
    expect(fitViewOptions).toBeDefined();
    expect(fitViewOptions).toMatch(/minZoom:\s*FIT_ZOOM_FLOOR/);
  });

  it("applies the same floor to the imperative refit() fitView call", () => {
    expect(src).toMatch(/fitView\(\{[^}]*minZoom:\s*FIT_ZOOM_FLOOR[^}]*\}\)/);
  });
});

// ─── Rank-distance wiring — long-range edges know how far they span ──────────
// ConditionEdge routes edges spanning at least LONG_RANGE_RANK_DISTANCE ranks
// as smooth-step instead of bezier; it can only do that if WorkerCanvas stamps
// rankDistance onto edge data after each layout pass, from useLayout's returned
// rank map. The threshold is named rather than repeated here because it has
// already moved once, and a number copied into a comment does not move with it.

describe("WorkerCanvas.tsx — source contract for rank-distance edge data", () => {
  const CANVAS_DIR = path.resolve(__dirname);
  const src = fs.readFileSync(path.join(CANVAS_DIR, "WorkerCanvas.tsx"), "utf-8");

  it("uses the layout's edges as they come, at both layout call sites", () => {
    const calls = src.match(/=\s*getLayoutedElements\(/g) ?? [];
    // Layout-on-mount effect + handleAutoLayout (editable canvas).
    expect(calls.length).toBeGreaterThanOrEqual(2);
    expect(src.match(/setEdges\(le\b/g) ?? []).toHaveLength(calls.length);
  });

  it("does not re-derive rank distance from the ASAP map", () => {
    // The map describes the graph and the stamped distance describes the
    // drawing, and a capped rank gap makes them disagree. Deriving here would
    // silently restore the map as the routing input.
    expect(src).not.toMatch(/rankDistance/);
    expect(src).not.toMatch(/\branks\b/);
  });
});

// ─── computeEffectiveNodeStatuses — legacy fallback + reconciliation ─────────
// The single per-node status derivation WorkerCanvas feeds to both the flow
// nodes' execStatus and the stage/follow-mode computations: nodeStatuses
// (live) wins per node, absent nodes fall back to the legacy
// execSteps/currentStep derivation, and the WHOLE resulting map is then run
// through reconcileNodeStatuses so a node can never render "running" once a
// descendant is terminal, and a terminal run never leaves a node looking
// like live work just because no signal ever arrived for it.

describe("computeEffectiveNodeStatuses", () => {
  const ids = ["a", "b", "c"];
  const edges = [
    { source: "a", target: "b" },
    { source: "b", target: "c" },
  ];

  it("nodeStatuses wins per node over the legacy fallback", () => {
    const result = computeEffectiveNodeStatuses(
      ids,
      edges,
      { a: "running", b: "pending", c: "pending" },
      [],
      null,
      false,
    );
    expect(result.a).toBe("running");
  });

  it("falls back to currentStep, then execSteps completedMap, then pending", () => {
    // b has no descendant terminal here (its only descendant is c, and c is
    // not in edges from b) — use a disjoint pair so descendant-suppression
    // doesn't interfere with proving the plain fallback precedence.
    const disjointEdges = [{ source: "x", target: "y" }];
    const result = computeEffectiveNodeStatuses(
      ["a", "b", "c"],
      disjointEdges,
      undefined,
      [{ step: "c", status: "completed" }],
      "b",
      false,
    );
    expect(result).toEqual({ a: "pending", b: "running", c: "completed" });
  });

  it("suppresses a stale 'running' ancestor once a descendant is terminal, live or not", () => {
    const result = computeEffectiveNodeStatuses(
      ids,
      edges,
      { a: "running", b: "running", c: "completed" },
      [],
      null,
      false,
    );
    // b's descendant c is terminal → b can no longer read "running".
    expect(result.b).toBe("completed");
    // a's descendant chain (b→c) also reaches a terminal node.
    expect(result.a).toBe("completed");
  });

  it("on a terminal run, a node with no signal at all reads as unknown (pending), not active", () => {
    const result = computeEffectiveNodeStatuses(
      ids,
      edges,
      { a: "completed" },
      [],
      null,
      true, // done
    );
    // b and c never got a signal; the run is done, so they must not look
    // like live work — they collapse to "pending" (absence of information).
    expect(result.b).toBe("pending");
    expect(result.c).toBe("pending");
  });

  it("on a terminal run, a node stuck 'queued'/'running' with no terminal signal collapses to pending", () => {
    const result = computeEffectiveNodeStatuses(
      ids,
      edges,
      { a: "completed", b: "running", c: "queued" },
      [],
      null,
      true,
    );
    expect(result.b).toBe("pending");
    expect(result.c).toBe("pending");
  });

  it("terminal statuses (completed/failed/escalated) pass through untouched on a done run", () => {
    const result = computeEffectiveNodeStatuses(
      ids,
      edges,
      { a: "completed", b: "failed", c: "escalated" },
      [],
      null,
      true,
    );
    expect(result).toEqual({ a: "completed", b: "failed", c: "escalated" });
  });
});

// ─── computeFollowCenter — viewport target for follow mode ───────────────────

describe("computeFollowCenter", () => {
  it("returns null when nothing is running", () => {
    const nodes = [{ id: "a", position: { x: 0, y: 0 } }];
    const statuses: Record<string, NodeExecStatus> = { a: "completed" };
    expect(computeFollowCenter(nodes, statuses)).toBeNull();
  });

  it("centers on the single running node, accounting for its size", () => {
    const nodes = [{ id: "a", position: { x: 100, y: 200 }, width: 210, height: 60 }];
    const statuses: Record<string, NodeExecStatus> = { a: "running" };
    expect(computeFollowCenter(nodes, statuses)).toEqual({ x: 100 + 105, y: 200 + 30 });
  });

  it("uses default dimensions when a node has no measured width/height yet", () => {
    const nodes = [{ id: "a", position: { x: 0, y: 0 } }];
    const statuses: Record<string, NodeExecStatus> = { a: "running" };
    expect(computeFollowCenter(nodes, statuses)).toEqual({ x: 105, y: 30 });
  });

  it("averages the centroid across multiple running nodes", () => {
    const nodes = [
      { id: "a", position: { x: 0, y: 0 }, width: 200, height: 40 },
      { id: "b", position: { x: 200, y: 0 }, width: 200, height: 40 },
    ];
    const statuses: Record<string, NodeExecStatus> = { a: "running", b: "running" };
    const center = computeFollowCenter(nodes, statuses);
    expect(center).toEqual({ x: (100 + 300) / 2, y: 20 });
  });

  it("ignores non-running nodes when computing the centroid", () => {
    const nodes = [
      { id: "a", position: { x: 0, y: 0 }, width: 200, height: 40 },
      { id: "b", position: { x: 1000, y: 1000 }, width: 200, height: 40 },
    ];
    const statuses: Record<string, NodeExecStatus> = { a: "running", b: "completed" };
    expect(computeFollowCenter(nodes, statuses)).toEqual({ x: 100, y: 20 });
  });
});

// ─── Source contract — new behaviors wired into the component itself ────────
// Mounting React Flow in vitest is heavy and not how this file tests
// WorkerCanvas elsewhere (see the MiniMap source-contract test above); these
// assert the component actually calls the primitives the behavior tests
// above exercise in isolation, and exposes the props/controls the contract
// requires.

describe("WorkerCanvas.tsx — source contract for progress/follow/selection wiring", () => {
  const CANVAS_DIR = path.resolve(__dirname);
  const src = fs.readFileSync(path.join(CANVAS_DIR, "WorkerCanvas.tsx"), "utf-8");

  it("declares live/done/onNodeSelect props", () => {
    expect(src).toMatch(/live\?: boolean/);
    expect(src).toMatch(/done\?: boolean/);
    expect(src).toMatch(/onNodeSelect\?: \(nodeId: string\) => void/);
  });

  it("fires onNodeSelect from the node click handler", () => {
    expect(src).toMatch(/onNodeSelect\?\.\(typedNode\.id\)/);
  });

  it("reserves layout height via computeReservedHeight, not the raw bbox height", () => {
    expect(src).toMatch(/computeReservedHeight\(/);
  });

  it("wires the follow-mode reducer and shouldAutoCenter gate", () => {
    expect(src).toMatch(
      /useReducer\(\s*followModeReducer,\s*initialFollowModeState\(live, done\),?\s*\)/,
    );
    expect(src).toMatch(/shouldAutoCenter\(followState, live, done\)/);
  });

  it("dispatches manual_interaction on onMoveStart (react-flow's pan/zoom-start hook)", () => {
    expect(src).toMatch(/onMoveStart=\{onMoveStart\}/);
    expect(src).toMatch(/dispatchFollow\(\{ type: "manual_interaction" \}\)/);
  });

  it("renders a visible Follow toggle that survives a manual interruption", () => {
    expect(src).toMatch(/dispatchFollow\(\{ type: "toggle" \}\)/);
    expect(src).toMatch(/followState\.following \? "Following" : "Follow"/);
  });

  it("derives the stage badge from computeStagePosition over the authored edges", () => {
    expect(src).toMatch(/computeStagePosition\(/);
    expect(src).toMatch(/Rank \{stagePosition\.stage\} of \{stagePosition\.totalStages\}/);
  });
});
