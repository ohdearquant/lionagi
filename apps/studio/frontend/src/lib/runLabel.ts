/**
 * Shared run display-label resolution.
 *
 * The backend already resolves `run.name` through the full priority chain —
 * show/play name > playbook name > agent-role descriptor > sanitized prompt
 * fallback (see lionagi/state/session_naming.py; a user-set label will slot
 * in ahead of all of those once that feature exists). This is the one place
 * every list/board surface reads that value from — LiveBoard.tsx and
 * recentGroups.ts previously each recomputed their own (weaker, and
 * disagreeing) fallback chain straight from playbook_name/agent_name,
 * ignoring the resolved name the backend already sent.
 */
import type { RunSummary } from "./types";

export function resolveRunLabel(run: RunSummary): string {
  return (
    run.name?.trim() ||
    run.show_play_name?.trim() ||
    run.playbook_name?.trim() ||
    run.agent_name?.trim() ||
    run.run_id.slice(-12)
  );
}
