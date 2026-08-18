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

/**
 * The pre-session-identity key for a row, when it differs from the current one.
 *
 * Attention dispositions are persisted against whatever key was emitted when
 * the operator resolved or snoozed the item. Rows written before routing moved
 * to the session `id` are keyed on `run_id`, so that value has to stay
 * readable or a discharged item comes back.
 */
export function legacyRunId(run: { id?: string | null; run_id?: string | null }): string | null {
  const legacy = run.run_id || "";
  return legacy && legacy !== runSessionId(run) ? legacy : null;
}
