/**
 * Session identity used by the current runs projection.
 *
 * `run_id` remains a compatibility field for older rows and clients. It may
 * describe a different or unavailable future execution identity, so routing,
 * list keys, and joins prefer the row's session `id` whenever it is present.
 */
export function runSessionId(run: { id?: string | null; run_id?: string | null }): string {
  // Empty counts as absent. `??` alone would let an empty `id` suppress a
  // perfectly good legacy `run_id`, and the empty string then travels on as a
  // route and a list key.
  return run.id || run.run_id || "";
}
