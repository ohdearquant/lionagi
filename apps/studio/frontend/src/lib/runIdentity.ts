/**
 * Session identity used by the current runs projection.
 *
 * The projection assigns `run_id` and `id` from one expression, so every row
 * carries one value under both names and this returns that value either way.
 * A server-side assertion holds it that way, and its message says that the
 * change giving a run a second identity is the change that must also land the
 * join reading both keys wherever a stored key was written from this one.
 */
export function runSessionId(run: { id?: string | null; run_id?: string | null }): string {
  // Empty counts as absent. `??` alone would let an empty `id` suppress a
  // perfectly good legacy `run_id`, and the empty string then travels on as a
  // route and a list key.
  return run.id || run.run_id || "";
}
