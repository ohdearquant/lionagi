// ADR-0021 §E: CIResultCard renders the lint/test/build/typecheck
// matrix with per-command timings — the layout from the spec.

import StatusPill from "@/components/StatusPill";

interface CIRunCommand {
  command: string;
  duration_seconds: number;
  passed: boolean;
}

interface CIResultContent {
  lint_passed?: boolean | null;
  tests_passed?: boolean | null;
  build_passed?: boolean | null;
  typecheck_passed?: boolean | null;
  test_count?: number | null;
  test_failures?: number | null;
  failure_summary?: string | null;
  commands?: CIRunCommand[];
  summary?: string;
  passed?: boolean | null;
}

interface CIResultCardProps {
  name: string;
  content: Record<string, unknown>;
}

function formatDuration(s: number): string {
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s - m * 60);
  return r > 0 ? `${m}m ${r}s` : `${m}m`;
}

function CheckRow({
  label,
  passed,
  detail,
}: {
  label: string;
  passed: boolean | null | undefined;
  detail?: string;
}) {
  if (passed == null) return null;
  return (
    <div className="flex items-center justify-between gap-3 py-1 text-body">
      <span className="text-content-primary">{label}</span>
      <span className="flex items-center gap-2">
        {detail ? <span className="tabular-nums text-content-secondary">{detail}</span> : null}
        <span
          className={"tabular-nums " + (passed ? "text-status-success" : "text-status-failure")}
        >
          {passed ? "passed" : "failed"}
        </span>
      </span>
    </div>
  );
}

export default function CIResultCard({ name, content }: CIResultCardProps) {
  const c = content as unknown as CIResultContent;
  const overall =
    c.passed ??
    [c.lint_passed, c.tests_passed, c.build_passed, c.typecheck_passed]
      .filter((v) => v != null)
      .every((v) => v === true);

  return (
    <div className="rounded border border-edge bg-surface-raised">
      <header className="flex items-center justify-between gap-2 border-b border-edge px-3 py-2">
        <div className="flex items-center gap-2">
          <StatusPill
            value={overall ? "approved" : "rejected"}
            label={`CI: ${overall ? "PASSED" : "FAILED"}`}
            taxonomy="verdict"
          />
          <span className="text-body text-content-primary">{name}</span>
        </div>
      </header>

      {c.summary ? (
        <div className="px-3 py-2 text-body text-content-secondary">{c.summary}</div>
      ) : null}

      <section className="border-t border-edge px-3 py-2">
        <CheckRow
          label="Tests"
          passed={c.tests_passed}
          detail={
            c.test_count != null
              ? c.test_failures != null
                ? `${c.test_count - c.test_failures} / ${c.test_count}`
                : `${c.test_count}`
              : undefined
          }
        />
        <CheckRow label="Lint" passed={c.lint_passed} />
        <CheckRow label="Typecheck" passed={c.typecheck_passed} />
        <CheckRow label="Build" passed={c.build_passed} />
      </section>

      {c.failure_summary ? (
        <section className="border-t border-edge px-3 py-2">
          <div className="mb-1 text-meta uppercase tracking-[0.06em] text-content-muted">
            Failure summary
          </div>
          <div className="text-body text-content-primary whitespace-pre-wrap">
            {c.failure_summary}
          </div>
        </section>
      ) : null}

      {c.commands && c.commands.length > 0 ? (
        <section className="border-t border-edge px-3 py-2">
          <div className="mb-1 text-meta uppercase tracking-[0.06em] text-content-muted">
            Commands
          </div>
          <ul className="space-y-1 text-body">
            {c.commands.map((cmd, i) => (
              <li key={i} className="flex items-center justify-between gap-3">
                <span className="font-mono text-content-primary truncate">{cmd.command}</span>
                <span className="flex items-center gap-2 shrink-0">
                  <span className="tabular-nums text-content-secondary">
                    {formatDuration(cmd.duration_seconds)}
                  </span>
                  <span className={cmd.passed ? "text-status-success" : "text-status-failure"}>
                    {cmd.passed ? "✓" : "✕"}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
