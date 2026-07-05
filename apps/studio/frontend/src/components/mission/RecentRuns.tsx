/**
 * Recent terminal runs strip — last 10 completed/failed/cancelled.
 * Compact mono rows: verdict glyph, name, duration, status.
 */

import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import StatusPill from "@/components/ui/StatusPill";
import Duration from "@/components/ui/Duration";
import type { RunSummary } from "@/lib/types";

interface Props {
  runs: RunSummary[];
  nowSec: number;
}

function durationSec(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  const end = run.ended_at ?? nowSec;
  return Math.max(0, end - run.started_at);
}

const KNOWN_STATUSES = new Set([
  "running",
  "completed",
  "failed",
  "cancelled",
  "pending",
  "queued",
  "timed_out",
  "aborted",
]);

export default function RecentRuns({ runs, nowSec }: Props) {
  const t = useTranslations("mission");
  const tStatus = useTranslations("history.status");

  // Localize known lifecycle statuses; unknown values fall back to the pill default
  const statusLabel = (status: string): string | undefined => {
    const s = status.toLowerCase();
    return KNOWN_STATUSES.has(s) ? tStatus(s as Parameters<typeof tStatus>[0]) : undefined;
  };

  return (
    <section aria-labelledby="recent-runs-heading">
      <div className="mb-2 flex items-center justify-between">
        <SectionLabel
          trailing={
            <Link
              to="/history"
              search={{ tab: "run" }}
              className="font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100"
            >
              {t("recent.viewAll")}
            </Link>
          }
        >
          <span id="recent-runs-heading">{t("recent.title")}</span>
        </SectionLabel>
      </div>

      {runs.length === 0 ? (
        <div className="rounded border border-edge bg-surface-raised px-4 py-3">
          <p className="text-[length:var(--t-sm)] text-content-muted">{t("recent.empty")}</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded border border-edge">
          {runs.map((run, idx) => {
            const name = run.playbook_name ?? run.agent_name ?? run.run_id.slice(-12);
            const dur = durationSec(run, nowSec);
            return (
              <Link
                key={run.run_id}
                to="/fleet"
                search={{ s: run.run_id }}
                className="flex items-center gap-3 bg-surface-raised px-3 py-1.5 transition-colors duration-100 hover:bg-surface-overlay"
                style={{ borderTop: idx === 0 ? undefined : "1px solid var(--edge-hairline)" }}
              >
                <StatusPill value={run.status} kind="lifecycle" label={statusLabel(run.status)} />
                <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-secondary">
                  {name}
                </span>
                <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
                  <Duration value={dur} />
                </span>
              </Link>
            );
          })}
        </div>
      )}
    </section>
  );
}
