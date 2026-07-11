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
import { computeEdgeSourceCompleted } from "./WorkerCanvas";

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
