/**
 * DESIGN-BRIEF §0: two chips side-by-side wherever a run is shown —
 * [status] [verdict]. Status always renders; verdict renders only when a
 * real verdict is supplied (never fabricated — see lib/runStatus.ts).
 */

import type { ReactNode } from "react";
import StatusPill from "@/components/ui/StatusPill";
import { deriveDisplayStatus, type RunStatusInput, type Verdict } from "@/lib/runStatus";

export interface StatusVerdictChipsProps {
  run: RunStatusInput;
  verdict?: Verdict;
  /** Caller-supplied i18n label for the status chip; falls back to StatusPill's own humanize(). */
  statusLabel?: ReactNode;
  className?: string;
}

export default function StatusVerdictChips({
  run,
  verdict = "none",
  statusLabel,
  className,
}: StatusVerdictChipsProps) {
  const displayStatus = deriveDisplayStatus(run);
  return (
    <span className={["inline-flex items-center gap-1", className].filter(Boolean).join(" ")}>
      <StatusPill value={displayStatus} kind="lifecycle" taxonomy="session" label={statusLabel} />
      {verdict !== "none" && <StatusPill value={verdict} kind="verdict" taxonomy="verdict" />}
    </span>
  );
}
