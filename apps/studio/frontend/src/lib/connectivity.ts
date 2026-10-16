/**
 * Tiny pub/sub so a network-level API failure anywhere in the app (fetch()
 * itself throwing — connection refused, DNS failure, CORS) can prompt
 * NoDaemonGate to re-probe /health immediately, instead of waiting out its
 * own poll interval. Decoupled from React so api.ts stays framework-agnostic.
 */

type Listener = () => void;

const listeners = new Set<Listener>();

export function reportConnectivityFailure(): void {
  for (const listener of listeners) listener();
}

export function onConnectivityFailure(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * A daemon that is running can still refuse this page: with no token
 * configured it answers 401 to a page on another origin, and with one it
 * answers 401 to a request without it. /health stays public either way, so the
 * gate's probe cannot see the refusal; API calls report it here instead.
 */
const refusalListeners = new Set<Listener>();

export function reportAccessRefused(): void {
  for (const listener of refusalListeners) listener();
}

export function onAccessRefused(listener: Listener): () => void {
  refusalListeners.add(listener);
  return () => refusalListeners.delete(listener);
}
