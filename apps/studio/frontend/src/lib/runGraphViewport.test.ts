/**
 * The run graph is fitted more than once while it is drawn: React Flow fits the
 * view from the update that measures the nodes it draws and reveals them, the
 * canvas schedules its own fit on the animation frame after it lays the nodes
 * out, and the embedding panel taking the layout's reported height resizes the
 * canvas, which fits once more. The baseline is taken from the view that holds
 * after those fits, not from one the canvas is about to replace.
 *
 * The graph here only changes when a frame runs, the way the real page only
 * paints on a frame, so a wait that spends a fixed interval instead of asking
 * for a frame can never see past the view it started on, however long it waits.
 * That is the failure a gap between two reads cannot rule out in the browser.
 */

import { describe, expect, it } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  parseRunGraphViewportTransform,
  settleRunGraphViewport,
  type RunGraphViewportRead,
} from "./runGraphViewport";

const INITIAL = "transform: translate(0px, 0px) scale(1);";
const FIRST_FIT = "transform: translate(50px, 20px) scale(0.5);";
const SETTLED = "transform: translate(112px, 80.5px) scale(1);";
const SETTLED_WITHOUT_SPACE = "transform: translate(112px,80.5px) scale(1);";

/**
 * A graph whose viewport takes its shape on the frame it is asked for, so
 * `viewAt` is a function of the number of frames run so far. A wait that asks
 * for no frame therefore reads one unchanging view however long it waits.
 */
function scriptedGraph(viewAt: (frame: number) => Partial<RunGraphViewportRead>): {
  read: () => Promise<RunGraphViewportRead>;
  nextFrame: () => Promise<void>;
  frames: () => number;
} {
  let frame = 0;
  return {
    read: () => Promise.resolve({ style: null, drawn: false, ...viewAt(frame) }),
    nextFrame: () => {
      frame += 1;
      return Promise.resolve();
    },
    frames: () => frame,
  };
}

describe("parseRunGraphViewportTransform", () => {
  it("reads translate and scale out of a comma-delimited transform", () => {
    expect(parseRunGraphViewportTransform(SETTLED)).toEqual({ x: 112, y: 80.5, zoom: 1 });
  });

  it("reads a transform the canvas wrote without a space after the comma", () => {
    expect(parseRunGraphViewportTransform("transform: translate(0px,0px) scale(1)")).toEqual({
      x: 0,
      y: 0,
      zoom: 1,
    });
  });

  it("reads a transform that carries a pan animation's transition", () => {
    expect(
      parseRunGraphViewportTransform(
        "transform: translate(0px, 0px) scale(1); transition: transform 250ms;",
      ),
    ).toEqual({ x: 0, y: 0, zoom: 1 });
  });

  it("rejects a spelling this canvas does not write rather than reading it as a view", () => {
    expect(() =>
      parseRunGraphViewportTransform("transform: translate3d(0px, 0px, 0px) scale(1)"),
    ).toThrow(/no transform/);
    expect(() => parseRunGraphViewportTransform("transform: matrix(1, 0, 0, 1, 0, 0)")).toThrow(
      /no transform/,
    );
  });

  it("rejects a transform whose numbers do not parse", () => {
    expect(() =>
      parseRunGraphViewportTransform("transform: translate(NaNpx, NaNpx) scale(NaN)"),
    ).toThrow(/unparseable/);
  });
});

describe("settleRunGraphViewport", () => {
  it("reads the view that holds after the canvas' later fit, not the one it replaces", async () => {
    const graph = scriptedGraph((frame) => ({
      style: frame < 2 ? INITIAL : frame < 5 ? FIRST_FIT : SETTLED,
      drawn: frame >= 1,
    }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(SETTLED);
  });

  it("does not read a view that held for fewer frames than the canvas' later fit can take", async () => {
    // The intermediate view holds for three frames; the canvas' later fit lands
    // on the fourth and replaces it. A read that settles in fewer frames than
    // the bound returns a view the graph is about to leave.
    const graph = scriptedGraph((frame) => ({
      style: frame <= 3 ? FIRST_FIT : SETTLED,
      drawn: true,
    }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(SETTLED);
  });

  it("does not read the graph until every node has been drawn", async () => {
    const graph = scriptedGraph((frame) => ({ style: SETTLED, drawn: frame >= 5 }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(SETTLED);
    expect(graph.frames()).toBeGreaterThanOrEqual(5);
  });

  it("does not count frames read before every node is drawn toward the hold", async () => {
    // React Flow's own fit lands with the last node's measurement, and this
    // canvas fits again a frame or two later, so the view read while the nodes
    // are still hidden is one that fit replaces. Those frames are not frames of
    // the hold: the view is read on the eighth frame here, not on the fourth the
    // hold would reach if it had counted them.
    const graph = scriptedGraph((frame) => ({
      style: frame < 5 ? INITIAL : SETTLED,
      drawn: frame >= 4,
    }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(SETTLED);
    expect(graph.frames()).toBe(8);
  });

  it("counts two spellings of one view as the same view", async () => {
    // This canvas writes the same view as `translate(112px, 80.5px) scale(1)` and
    // as `translate(112px,80.5px) scale(1)`. The transform is parsed into its
    // numbers, so alternating the spellings does not restart the hold; comparing
    // the serialized strings would never settle on this graph.
    const spellings = [SETTLED, SETTLED_WITHOUT_SPACE];
    const graph = scriptedGraph((frame) => ({ style: spellings[frame % 2], drawn: true }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(SETTLED);
    expect(graph.frames()).toBe(4);
  });

  it("restarts the hold when the viewport leaves the screen mid-stream", async () => {
    const graph = scriptedGraph((frame) => ({
      style: frame === 3 ? null : SETTLED,
      drawn: true,
    }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(SETTLED);
    expect(graph.frames()).toBe(7);
  });

  it("reads a graph the canvas fitted to React Flow's own initial viewport", async () => {
    const graph = scriptedGraph(() => ({ style: INITIAL, drawn: true }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(INITIAL);
  });

  it("treats a viewport that is not on screen yet as not settled", async () => {
    const graph = scriptedGraph((frame) => ({
      style: frame < 2 ? null : SETTLED,
      drawn: frame >= 2,
    }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).resolves.toBe(SETTLED);
  });

  it("rejects a style whose transform it does not recognise rather than waiting it out", async () => {
    const graph = scriptedGraph(() => ({
      style: "transform: translate3d(0px, 0px, 0px) scale(1)",
      drawn: true,
    }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 2_000 }),
    ).rejects.toThrow(/no transform/);
  });

  it("fails rather than reading a view that never comes to rest", async () => {
    const graph = scriptedGraph((frame) => ({
      style: frame % 2 === 0 ? FIRST_FIT : SETTLED,
      drawn: true,
    }));

    await expect(
      settleRunGraphViewport(graph.read, graph.nextFrame, { timeoutMs: 60 }),
    ).rejects.toThrow(/never settled/);
  });
});

describe("the run graph spec's wait", () => {
  const spec = fs.readFileSync(
    path.join(__dirname, "..", "..", "e2e", "interaction-wave.spec.ts"),
    "utf-8",
  );

  it("takes its baseline through the frame-paced settle, not a fixed interval", () => {
    expect(spec).toMatch(/settleRunGraphViewport\(/);
    expect(spec).not.toMatch(/intervals:\s*\[250\]/);
  });

  it("counts the graph drawn from the nodes React Flow has revealed", () => {
    expect(spec).toMatch(/visibility !== "hidden"/);
  });

  it("reads the viewport's style and its nodes in one round trip", () => {
    // Read separately, a fit landing between the two reads reports a settled
    // transform beside the nodes it drew, which is not a state the page held.
    expect(spec).toMatch(/inlineViewport\.evaluate\(/);
    expect(spec).not.toMatch(/await inlineViewport\.getAttribute\("style"\)/);
    expect(spec).not.toMatch(/evaluateAll\(/);
  });

  it("waits on the page's animation frames between reads", () => {
    expect(spec).toMatch(/requestAnimationFrame/);
  });
});
