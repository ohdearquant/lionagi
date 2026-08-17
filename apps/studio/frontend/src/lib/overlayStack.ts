/**
 * Which overlay currently owns the keyboard.
 *
 * Every modal surface traps Tab and Escape with its own listener on document or
 * window, and those listeners fire in the order they were added, so the overlay
 * that opened FIRST sees the key first. Its trap then finds focus sitting
 * outside itself, which is exactly what a newer overlay stacked on top looks
 * like, and it pulls focus back out of the surface the operator is actually
 * using. `preventDefault()` does not help: the older listener has already run,
 * and stopping propagation there would break the newer one instead.
 *
 * Registering here gives every trap one question to ask before it acts: am I
 * the overlay the operator is looking at?
 *
 * Answering that from registration order alone would be wrong, because
 * registration order is mount order and mount order is not what the operator
 * sees. Every overlay here is `fixed` at the same z-index and none of them
 * portals, so paint order is document order, and AppShell renders the command
 * palette after the routed view rather than inside it. The palette therefore
 * draws above any modal belonging to a route no matter which mounted first: a
 * modal that mounts while the palette is open registers later and would claim
 * the keyboard while sitting visually underneath it.
 *
 * So ownership is ordered by paint layer first and registration second. Within
 * one layer the newest registration wins, which is the mount-order rule that
 * was right all along for overlays that really do stack on each other.
 */

/**
 * Paint layers, in the order they draw. A layer exists here only because
 * something in the shell already fixes that overlay's paint position; this
 * declares that fact where the keyboard decision is made rather than leaving
 * it implicit in AppShell's JSX order.
 */
export const OverlayLayer = {
  /** Anything rendered inside the routed view. */
  Routed: 0,
  /** Rendered by AppShell after the routed view, so it always draws above it. */
  Shell: 1,
} as const;

export type OverlayLayer = (typeof OverlayLayer)[keyof typeof OverlayLayer];

interface Registration {
  token: symbol;
  layer: OverlayLayer;
}

const stack: Registration[] = [];

/** Claim the keyboard. Call from the same effect that adds the key listener,
 *  and release with `popOverlay` in that effect's cleanup. Pass the layer the
 *  overlay paints on; the default suits anything inside the routed view. */
export function pushOverlay(
  description: string,
  layer: OverlayLayer = OverlayLayer.Routed,
): symbol {
  const token = Symbol(description);
  stack.push({ token, layer });
  return token;
}

export function popOverlay(token: symbol): void {
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    if (stack[i].token === token) {
      stack.splice(i, 1);
      return;
    }
  }
}

/** True when nothing is painted above this overlay: no registration on a
 *  higher layer, and none newer on its own. An overlay that never registered
 *  is not topmost, which fails toward leaving the key alone.
 *
 *  Linear in the overlays open at once. Caching the winner would add an
 *  invalidation rule to every push and pop for no gain at that size. */
export function isTopmostOverlay(token: symbol): boolean {
  let owner: Registration | undefined;
  for (const registration of stack) {
    // `>=` so the last registration on the winning layer takes it.
    if (!owner || registration.layer >= owner.layer) owner = registration;
  }
  return owner !== undefined && owner.token === token;
}
