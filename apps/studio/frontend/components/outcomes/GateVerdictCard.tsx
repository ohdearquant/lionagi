// ADR-0021 §E: GateVerdictCard renders the pass/fail outcome of a
// gate (play-gate, show-gate, etc.) with feedback and notes surfaced
// alongside, not buried in a text field.

import StatusPill from "@/components/StatusPill";

interface GateVerdictContent {
  gate_passed?: boolean;
  feedback?: string | null;
  notes?: string | null;
  summary?: string;
  passed?: boolean | null;
}

interface GateVerdictCardProps {
  name: string;
  content: Record<string, unknown>;
}

export default function GateVerdictCard({
  name,
  content,
}: GateVerdictCardProps) {
  const c = content as unknown as GateVerdictContent;
  const passed = c.gate_passed ?? c.passed ?? null;
  const verdictLabel =
    passed === true ? "ACCEPT" : passed === false ? "REJECT" : "PENDING";

  return (
    <div className="rounded border border-edge bg-surface-raised">
      <header className="flex items-center justify-between gap-2 border-b border-edge px-3 py-2">
        <div className="flex items-center gap-2">
          <StatusPill
            value={passed === true ? "approved" : passed === false ? "rejected" : ""}
            label={`GATE: ${verdictLabel}`}
            taxonomy="verdict"
          />
          <span className="text-body text-content-primary">{name}</span>
        </div>
      </header>

      {c.summary ? (
        <div className="px-3 py-2 text-body text-content-secondary">
          {c.summary}
        </div>
      ) : null}

      {c.feedback ? (
        <section className="border-t border-edge px-3 py-2">
          <div className="mb-1 text-meta uppercase tracking-[0.06em] text-content-muted">
            Feedback
          </div>
          <div className="text-body text-content-primary whitespace-pre-wrap">
            {c.feedback}
          </div>
        </section>
      ) : null}

      {c.notes ? (
        <section className="border-t border-edge px-3 py-2">
          <div className="mb-1 text-meta uppercase tracking-[0.06em] text-content-muted">
            Notes
          </div>
          <div className="text-body text-content-secondary whitespace-pre-wrap">
            {c.notes}
          </div>
        </section>
      ) : null}
    </div>
  );
}
