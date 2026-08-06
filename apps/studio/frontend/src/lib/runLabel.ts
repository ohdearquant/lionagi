/**
 * The one place a run's human-facing label is chosen.
 *
 * A user's rename is stored server-side and served as `display_name`, resolved
 * by a single backend priority chain. Every list has to read that field or a
 * renamed session keeps its old label everywhere except the run header, which
 * is the same as the rename not working.
 */

/** The run fields this needs; kept structural so both RunSummary and the
 *  reducers' narrower row shapes satisfy it. */
export interface RunLabelSource {
  run_id: string;
  display_name?: string | null;
  playbook_name?: string | null;
  agent_name?: string | null;
}

/**
 * Pick the label for a run.
 *
 * `display_name` wins when it carries anything. Blank counts as absent — the
 * same rule the backend applies to its own candidates, and load-bearing here
 * because the resolver's last resort returns an empty string rather than null
 * for a row with no id, and `"" ?? fallback` keeps the empty string.
 *
 * The older per-field chain stays as the fallback so a frontend talking to a
 * daemon that predates `display_name` behaves exactly as it did before.
 *
 * @param idTailLength how many trailing id characters the caller's layout has
 *   room for; callers already differ (compact cards use fewer).
 */
export function runLabel(run: RunLabelSource, idTailLength = 12): string {
  const resolved = run.display_name;
  if (typeof resolved === "string" && resolved.trim()) return resolved.trim();
  return run.playbook_name ?? run.agent_name ?? run.run_id.slice(-idTailLength);
}
