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
