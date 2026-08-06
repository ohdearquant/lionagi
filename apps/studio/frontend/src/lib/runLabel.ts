/**
 * Shared run display-label resolution.
 *
 * The backend already resolves `run.name` through the full priority chain —
 * show/play name > playbook name > agent-role descriptor > sanitized prompt
 * fallback (see lionagi/state/session_naming.py; a user-set label will slot
 * in ahead of all of those once that feature exists). This is the one place
 * every list/board surface reads that value from — every other file under
 * components/ that previously recomputed its own (weaker, and disagreeing)
 * fallback chain straight from playbook_name/agent_name has been converted
 * to call this instead, so a run can no longer show two different names on
 * two different surfaces at once.
 *
 * A non-empty `run.name` is rendered VERBATIM, never rewritten. There is no
 * reliable way to tell a backend-resolved agent-role label ("implementer ·
 * 14:22", baked in UTC — see agent_role_label's docstring) apart from a
 * future user-set custom name that happens to share that shape by text
 * alone, and a stored/custom name must never be mutated. The one place this
 * function computes local-time HH:MM itself is its *own* fallback tier below
 * — building a label from a bare `agent_name` + `started_at` when the
 * backend sent no name at all, a case where this function controls the
 * construction end to end and there is nothing stored to disagree with.
 */
import type { RunSummary } from "./types";

function localHHMM(startedAt: number | null | undefined): string | null {
  if (startedAt == null) return null;
  const d = new Date(startedAt * 1000);
  if (Number.isNaN(d.getTime())) return null;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

export function resolveRunLabel(run: RunSummary): string {
  const backendName = run.name?.trim();
  if (backendName) {
    return backendName;
  }

  const showPlayName = run.show_play_name?.trim();
  if (showPlayName) return showPlayName;

  const playbookName = run.playbook_name?.trim();
  if (playbookName) return playbookName;

  const agentName = run.agent_name?.trim();
  if (agentName) {
    const local = localHHMM(run.started_at);
    return local ? `${agentName} · ${local}` : agentName;
  }

  return run.run_id.slice(-12);
}
