/**
 * Taking the inline run graph's viewport transform once its view has settled.
 *
 * The graph is fitted more than once while it is drawn: React Flow fits the view
 * from the update that measures the nodes it draws and reveals them, and this
 * canvas fits again on a later animation frame, once the embedding panel has
 * taken the layout's reported height. A transform read while that later fit is
 * still pending is not the view the graph settles on.
 *
 * The wait between reads is paced by animation frames rather than by wall-clock
 * time. A fit is itself scheduled on a frame, and frame callbacks run in the
 * order they were registered, so a fit already pending when the next read is
 * scheduled runs before it; a fixed interval between reads can instead land
 * between two fits and hand back the same, unsettled view twice.
 *
 * A transform is not a string two reads can be compared on either: this canvas
 * writes the same view as `translate(112px, 80.5px) scale(1)` and as
 * `translate(112px,80.5px) scale(1)`, and a pan animation appends a
 * `transition` to the same style. The transform is parsed into its translate and
 * scale numbers before it is compared.
 */

export interface RunGraphViewportTransform {
  x: number;
  y: number;
  zoom: number;
}

/** One read of the graph: its viewport's style, and whether it is all drawn. */
export interface RunGraphViewportRead {
  /** The viewport's serialized style, or null before it is on screen. */
  style: string | null;
  /**
   * Whether every node the graph renders has been measured and revealed. React
   * Flow reveals a node in the same update that records its measured size, and
   * fits the view from that update once every node is measured, so a fully drawn
   * graph is one React Flow has already fitted.
   */
  drawn: boolean;
}

/** Consecutive frames a transform must hold across before it is read. */
export const SETTLED_RUN_GRAPH_FRAMES = 3;

/** How long to wait for the view to come to rest, in milliseconds. */
export const SETTLE_RUN_GRAPH_TIMEOUT_MS = 5_000;

// Matched inside the style rather than against the whole of it, so a transform
// carrying a pan animation's transition is still read.
const TRANSFORM_PATTERN = /translate\(([^,]+),\s*([^)]+)\)\s*scale\(([^)]+)\)/;

/**
 * The viewport's translate and scale, parsed out of the canvas' style.
 *
 * `translate3d` and `matrix` are not spellings this canvas writes, and a
 * transform whose numbers do not parse is not a view either: both are errors
 * rather than something to wait out, so a style this reader does not recognise
 * cannot be read as a settled viewport.
 */
export function parseRunGraphViewportTransform(style: string): RunGraphViewportTransform {
  const parts = TRANSFORM_PATTERN.exec(style);
  if (parts === null) {
    throw new Error(`run graph viewport has no transform: ${style}`);
  }
  const [x, y, zoom] = [parts[1], parts[2], parts[3]].map((part) => Number.parseFloat(part));
  if (![x, y, zoom].every(Number.isFinite)) {
    throw new Error(`run graph viewport has an unparseable transform: ${style}`);
  }
  return { x, y, zoom };
}

export interface SettleRunGraphViewportOptions {
  /** Consecutive frames a transform must hold across. */
  stableFrames?: number;
  /** How long to wait for the view to come to rest, in milliseconds. */
  timeoutMs?: number;
}

/**
 * Waits for the run graph's viewport to come to rest and resolves with its
 * serialized style.
 *
 * The read and the frame are injected, so the same wait paces a page — a real
 * animation frame, the graph's real nodes — and a test's scripted reads.
 *
 * The viewport is read once every node is drawn and the transform has held
 * across frames. The graph being drawn is what says React Flow's own fit has
 * run; holding is what says no later fit is still pending. Holding decides the
 * read rather than a difference from any particular transform, because a fit
 * whose result is React Flow's initial view is still a fit, and requiring the
 * transform to move away from it would fail a graph that lands there.
 *
 * A viewport that is not on screen yet is not a settled read. A style whose
 * transform is not one this reader parses is an error rather than something to
 * wait out, so an unrecognised spelling cannot pass as settled, and a view that
 * never comes to rest fails here rather than being read.
 */
export async function settleRunGraphViewport(
  read: () => Promise<RunGraphViewportRead>,
  nextFrame: () => Promise<void>,
  options: SettleRunGraphViewportOptions = {},
): Promise<string> {
  const requiredFrames = options.stableFrames ?? SETTLED_RUN_GRAPH_FRAMES;
  const deadline = Date.now() + (options.timeoutMs ?? SETTLE_RUN_GRAPH_TIMEOUT_MS);
  let previous: RunGraphViewportTransform | null = null;
  let held = 0;
  let lastRead = "nothing on screen";
  for (;;) {
    await nextFrame();
    const { style, drawn } = await read();
    if (style === null) {
      previous = null;
      held = 0;
    } else {
      lastRead = style;
      const current = parseRunGraphViewportTransform(style);
      const unchanged =
        previous !== null &&
        current.x === previous.x &&
        current.y === previous.y &&
        current.zoom === previous.zoom;
      previous = current;
      held = unchanged ? held + 1 : 0;
      if (drawn && held >= requiredFrames) {
        return style;
      }
    }
    if (Date.now() > deadline) {
      throw new Error(`the run graph never settled with every node drawn; last read: ${lastRead}`);
    }
  }
}
