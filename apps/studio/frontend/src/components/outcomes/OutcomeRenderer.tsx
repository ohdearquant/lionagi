// ADR-0021 §E: kind-dispatched renderer for skill outcomes.
// Falls back to a JSON viewer for unknown kinds so new outcome types
// degrade gracefully until a dedicated card is added.

import type { ArtifactSummary } from "@/lib/api";
import ReviewVerdictCard from "./ReviewVerdictCard";
import GateVerdictCard from "./GateVerdictCard";
import CIResultCard from "./CIResultCard";

export interface OutcomeRendererProps {
  artifact: ArtifactSummary;
}

export default function OutcomeRenderer({ artifact }: OutcomeRendererProps) {
  const content = artifact.content ?? {};
  switch (artifact.kind) {
    case "review_verdict":
      return (
        <ReviewVerdictCard name={artifact.name} content={content as Record<string, unknown>} />
      );
    case "gate_verdict":
      return <GateVerdictCard name={artifact.name} content={content as Record<string, unknown>} />;
    case "ci_result":
      return <CIResultCard name={artifact.name} content={content as Record<string, unknown>} />;
    default:
      return (
        <div className="rounded border border-edge bg-surface-raised p-3">
          <div className="mb-2 flex items-center justify-between text-meta uppercase tracking-[0.06em] text-content-muted">
            <span>{artifact.kind}</span>
            <span className="font-mono">{artifact.id.slice(0, 8)}</span>
          </div>
          <div className="mb-1 text-body text-content-primary">{artifact.name}</div>
          <pre className="max-h-64 overflow-auto rounded bg-surface-base p-2 text-meta font-mono text-content-secondary">
            {JSON.stringify(content, null, 2)}
          </pre>
        </div>
      );
  }
}
