/**
 * Which overlay owns the keyboard. Key listeners fire in the order they were
 * added, so the oldest trap acts first and drags focus out of whatever opened
 * on top of it; this gives every trap one question to ask before it acts.
 * Ordered by paint layer first, since mount order is not what the operator
 * sees, then newest registration within a layer.
 */

/** Paint layers, in draw order, declared where the keyboard decision is made. */
export const OverlayLayer = {
  /** Anything rendered inside the routed view. */
  Routed: 0,
  /** Rendered after the routed view, so it always draws above it. */
  Shell: 1,
} as const;

export type OverlayLayer = (typeof OverlayLayer)[keyof typeof OverlayLayer];

interface Registration {
  token: symbol;
  layer: OverlayLayer;
}

const stack: Registration[] = [];

/** Claim the keyboard from the effect that adds the key listener; release with `popOverlay` in its cleanup. */
export function pushOverlay(
  description: string,
  layer: OverlayLayer = OverlayLayer.Routed,
): symbol {
  const token = Symbol(description);
  stack.push({ token, layer });
  notifyOverlayChange();
  return token;
}

export function popOverlay(token: symbol): void {
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    if (stack[i].token === token) {
      stack.splice(i, 1);
      notifyOverlayChange();
      return;
    }
  }
}

const listeners = new Set<() => void>();

/** Ownership changes without the overlay beneath re-rendering, so it has to be told. */
export function subscribeOverlayChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// Held while at least one overlay is registered, so a nested overlay closing does
// not hand scrolling back while the one beneath is still open. Null means unheld;
// the saved value is restored verbatim rather than cleared, since the page may
// have been setting its own overflow.
let heldBodyOverflow: string | null = null;

function syncBackgroundIsolation(): void {
  if (stack.length > 0) {
    if (heldBodyOverflow === null) {
      heldBodyOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    return;
  }
  if (heldBodyOverflow !== null) {
    document.body.style.overflow = heldBodyOverflow;
    heldBodyOverflow = null;
  }
}

function notifyOverlayChange(): void {
  syncBackgroundIsolation();
  for (const listener of Array.from(listeners)) listener();
}

/** True when nothing paints above this overlay. An unregistered one is not topmost, which leaves the key alone. */
export function isTopmostOverlay(token: symbol): boolean {
  let owner: Registration | undefined;
  for (const registration of stack) {
    // `>=` so the last registration on the winning layer takes it.
    if (!owner || registration.layer >= owner.layer) owner = registration;
  }
  return owner !== undefined && owner.token === token;
}
