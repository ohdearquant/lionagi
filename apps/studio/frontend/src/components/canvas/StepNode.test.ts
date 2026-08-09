/**
 * computeNodeVisualStyle — status precedence at the readability zoom floor.
 *
 * At the minimum fit zoom (0.1) a node card renders too small to read label
 * text or the pulse-ring animation reliably, so the precedence between
 * running/completed/failed/pending must survive on non-animation cues alone:
 * border weight and a left-edge status rail color. This file pins that
 * contract without mounting React Flow.
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { computeNodeVisualStyle } from "./StepNode";
import type { NodeExecStatus } from "./StepNode";

describe("computeNodeVisualStyle", () => {
  it("running is the strongest: thickest border and a non-transparent rail", () => {
    const running = computeNodeVisualStyle("running", false);
    expect(running.borderWidth).toBe(3);
    expect(running.railColor).not.toBe("transparent");
  });

  it("pending recedes: thinnest border and no rail color", () => {
    const pending = computeNodeVisualStyle("pending", false);
    expect(pending.borderWidth).toBe(1);
    expect(pending.railColor).toBe("transparent");
  });

  it("queued renders identically to pending — a correctness distinction, not a visual one", () => {
    expect(computeNodeVisualStyle("queued", false)).toEqual(
      computeNodeVisualStyle("pending", false),
    );
  });

  it("running strictly outweighs pending in border weight", () => {
    const running = computeNodeVisualStyle("running", false);
    const pending = computeNodeVisualStyle("pending", false);
    expect(running.borderWidth).toBeGreaterThan(pending.borderWidth);
  });

  it("completed is moderate: thicker than pending, and its own rail color", () => {
    const completed = computeNodeVisualStyle("completed", false);
    const pending = computeNodeVisualStyle("pending", false);
    expect(completed.borderWidth).toBeGreaterThan(pending.borderWidth);
    expect(completed.borderWidth).toBeLessThanOrEqual(3);
    expect(completed.railColor).not.toBe("transparent");
  });

  it("escalated uses the warning visual while failed keeps the failure visual", () => {
    const escalated = computeNodeVisualStyle("escalated", false);
    const failed = computeNodeVisualStyle("failed", false);
    const warning = computeNodeVisualStyle("awaiting_approval", false);

    expect(escalated).toEqual(warning);
    expect(escalated).not.toEqual(failed);
    expect(failed.borderColor).toBe("var(--dag-failed-border)");
    expect(failed.railColor).toBe("var(--dag-failed-border)");
  });

  it("running, completed, and failed are all mutually distinguishable by color at any zoom", () => {
    const running = computeNodeVisualStyle("running", false);
    const completed = computeNodeVisualStyle("completed", false);
    const failed = computeNodeVisualStyle("failed", false);
    const colors = [running, completed, failed].map((v) => `${v.borderColor}|${v.railColor}`);
    expect(new Set(colors).size).toBe(3);
  });

  it("running outweighs completed in border weight, so the frontier draws the eye first", () => {
    const running = computeNodeVisualStyle("running", false);
    const completed = computeNodeVisualStyle("completed", false);
    expect(running.borderWidth).toBeGreaterThan(completed.borderWidth);
  });

  it("awaiting_approval and paused share the warn visual, distinct from pending", () => {
    const awaiting = computeNodeVisualStyle("awaiting_approval", false);
    const paused = computeNodeVisualStyle("paused", false);
    const pending = computeNodeVisualStyle("pending", false);
    expect(awaiting).toEqual(paused);
    expect(awaiting.railColor).not.toBe(pending.railColor);
  });

  it("every status produces a defined, non-empty rail entry (transparent counts as defined)", () => {
    const statuses: NodeExecStatus[] = [
      "pending",
      "queued",
      "running",
      "awaiting_approval",
      "paused",
      "completed",
      "failed",
      "escalated",
    ];
    for (const status of statuses) {
      const visual = computeNodeVisualStyle(status, false);
      expect(visual.railColor).toBeTruthy();
      expect(visual.borderWidth).toBeGreaterThanOrEqual(1);
    }
  });
});

describe("StepNode.tsx — source contract for the status rail + reduced-motion", () => {
  const CANVAS_DIR = path.resolve(__dirname);
  const src = fs.readFileSync(path.join(CANVAS_DIR, "StepNode.tsx"), "utf-8");

  it("renders a left-edge status rail driven by the visual style, not a static class", () => {
    expect(src).toMatch(/background: visual\.railColor/);
  });

  it("gates the pulse animation on prefers-reduced-motion, keeping the rail/border cues animation-free", () => {
    expect(src).toMatch(/usePrefersReducedMotion/);
    expect(src).toMatch(/reducedMotion \? "" : " animate-pulse"/);
  });
});
