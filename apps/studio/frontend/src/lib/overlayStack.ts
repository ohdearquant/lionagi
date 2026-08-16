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
 * Registering here gives every trap one question to ask before it acts. The
 * newest registration wins, which matches what the operator sees on screen.
 */

const stack: symbol[] = [];

/** Claim the keyboard. Call from the same effect that adds the key listener,
 *  and release with `popOverlay` in that effect's cleanup, so the stack order
 *  matches the order the overlays actually mounted. */
export function pushOverlay(description: string): symbol {
  const token = Symbol(description);
  stack.push(token);
  return token;
}

export function popOverlay(token: symbol): void {
  const index = stack.lastIndexOf(token);
  if (index !== -1) stack.splice(index, 1);
}

/** True when nothing is stacked above this overlay. An overlay that never
 *  registered is not topmost, which fails toward leaving the key alone. */
export function isTopmostOverlay(token: symbol): boolean {
  return stack.length > 0 && stack[stack.length - 1] === token;
}
