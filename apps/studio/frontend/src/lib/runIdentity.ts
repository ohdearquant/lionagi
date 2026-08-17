/**
 * Session identity used by the current runs projection.
 *
 * `run_id` remains a compatibility field for older rows and clients. It may
 * describe a different or unavailable future execution identity, so routing,
 * list keys, and joins prefer the row's session `id` whenever it is present.
 */
export function runSessionId(run: { id?: string | null; run_id?: string | null }): string {
  return run.id ?? run.run_id ?? "";
}
