// ADR-0021 §E: ReviewVerdictCard renders the structured verdict as a
// severity/category breakdown with blocking findings highlighted, not
// as raw text. Mirrors the spec layout in the ADR.

import StatusPill from "@/components/StatusPill";

interface Finding {
  severity: "critical" | "high" | "medium" | "low" | "info";
  category: string;
  file?: string | null;
  line?: number | null;
  description: string;
  suggestion?: string | null;
}

interface ReviewVerdictContent {
  verdict?: string;
  summary?: string;
  findings?: Finding[];
  round?: number;
  passed?: boolean | null;
}

interface ReviewVerdictCardProps {
  name: string;
  content: Record<string, unknown>;
}

const SEVERITY_ORDER: Finding["severity"][] = ["critical", "high", "medium", "low", "info"];
const BLOCKING_SEVERITIES = new Set<Finding["severity"]>(["critical", "high"]);

const SEVERITY_LABEL: Record<Finding["severity"], string> = {
  critical: "Critical",
  high: "Major",
  medium: "Minor",
  low: "Low",
  info: "Info",
};

export default function ReviewVerdictCard({ name, content }: ReviewVerdictCardProps) {
  const c = content as unknown as ReviewVerdictContent;
  const verdict = c.verdict ?? "UNKNOWN";
  const findings = c.findings ?? [];

  // Severity counts (sorted descending by severity).
  const counts: Record<Finding["severity"], number> = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    info: 0,
  };
  const categoryCounts: Record<string, number> = {};
  for (const f of findings) {
    counts[f.severity] += 1;
    categoryCounts[f.category] = (categoryCounts[f.category] ?? 0) + 1;
  }

  const blocking = findings.filter((f) => BLOCKING_SEVERITIES.has(f.severity));
  const minor = findings.filter((f) => !BLOCKING_SEVERITIES.has(f.severity));

  return (
    <div className="rounded border border-edge bg-surface-raised">
      <header className="flex items-center justify-between gap-2 border-b border-edge px-3 py-2">
        <div className="flex items-center gap-2">
          <StatusPill value={verdict} taxonomy="verdict" />
          <span className="text-body text-content-primary">{name}</span>
          {c.round != null ? (
            <span className="text-meta text-content-muted">round {c.round}</span>
          ) : null}
        </div>
      </header>

      {c.summary ? (
        <div className="px-3 py-2 text-body text-content-secondary">{c.summary}</div>
      ) : null}

      <div className="grid grid-cols-2 gap-3 border-t border-edge px-3 py-2">
        <div>
          <div className="mb-1 text-meta uppercase tracking-[0.06em] text-content-muted">
            Findings
          </div>
          <div className="flex flex-wrap gap-2 text-body">
            {SEVERITY_ORDER.map((s) =>
              counts[s] > 0 ? (
                <span key={s} className="tabular-nums">
                  <span className="text-content-primary">{SEVERITY_LABEL[s]}</span>{" "}
                  <span className="text-content-secondary">{counts[s]}</span>
                </span>
              ) : null,
            )}
            {findings.length === 0 ? (
              <span className="text-meta text-content-muted">no findings</span>
            ) : null}
          </div>
        </div>
        <div>
          <div className="mb-1 text-meta uppercase tracking-[0.06em] text-content-muted">
            Categories
          </div>
          <div className="flex flex-wrap gap-2 text-body">
            {Object.entries(categoryCounts).map(([cat, n]) => (
              <span key={cat} className="tabular-nums">
                <span className="text-content-primary">{cat}</span>{" "}
                <span className="text-content-secondary">{n}</span>
              </span>
            ))}
            {Object.keys(categoryCounts).length === 0 ? (
              <span className="text-meta text-content-muted">—</span>
            ) : null}
          </div>
        </div>
      </div>

      {blocking.length > 0 ? (
        <section className="border-t border-edge px-3 py-2">
          <div className="mb-1 text-meta uppercase tracking-[0.06em] text-content-muted">
            Blocking findings
          </div>
          <ul className="space-y-2">
            {blocking.map((f, i) => (
              <li
                key={i}
                className="rounded border border-status-error/30 bg-status-error-bg/40 p-2"
              >
                <div className="flex items-center gap-2">
                  <span className="rounded bg-status-error/10 px-1.5 py-0.5 text-meta uppercase tracking-wide text-status-error">
                    {SEVERITY_LABEL[f.severity]}
                  </span>
                  <span className="text-body text-content-primary">{f.description}</span>
                </div>
                {f.file ? (
                  <div className="mt-1 font-mono text-meta text-content-muted">
                    {f.file}
                    {f.line != null ? `:${f.line}` : ""}
                  </div>
                ) : null}
                {f.suggestion ? (
                  <div className="mt-1 text-body text-content-secondary">
                    <span className="text-meta uppercase tracking-[0.06em] text-content-muted">
                      Suggestion
                    </span>{" "}
                    {f.suggestion}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {minor.length > 0 ? (
        <details className="border-t border-edge px-3 py-2">
          <summary className="cursor-pointer text-meta uppercase tracking-[0.06em] text-content-muted">
            Suggestions ({minor.length})
          </summary>
          <ul className="mt-2 space-y-1 text-body text-content-secondary">
            {minor.map((f, i) => (
              <li key={i}>
                <span className="text-meta uppercase tracking-[0.06em] text-content-muted">
                  {SEVERITY_LABEL[f.severity]}
                </span>{" "}
                {f.description}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
