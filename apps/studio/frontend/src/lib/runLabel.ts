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
 * The agent-role tier ("<agent> · HH:MM") is the one part of the resolved
 * name that is clock-shaped, and Studio otherwise always shows the viewer's
 * local time. The backend bakes that HH:MM in UTC so the *stored* value is
 * deterministic regardless of which machine resolved it (see
 * agent_role_label's docstring) — this function recomputes the HH:MM half
 * from `run.started_at` in the browser's local time before display, both
 * when the backend already sent a resolved agent-role name and when this
 * function falls back to building one itself from a bare `agent_name`.
 */
import type { RunSummary } from "./types";

const AGENT_TIME_SUFFIX_RE = / · \d{2}:\d{2}$/;

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
    const agentName = run.agent_name?.trim();
    if (
      agentName &&
      backendName.startsWith(`${agentName} · `) &&
      AGENT_TIME_SUFFIX_RE.test(backendName)
    ) {
      const local = localHHMM(run.started_at);
      if (local) return `${agentName} · ${local}`;
    }
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
