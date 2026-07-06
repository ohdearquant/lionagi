/**
 * Recent terminal runs strip — last 10 completed/failed/cancelled.
 * Compact mono rows: verdict glyph, name, duration, status.
 *
 * Consecutive runs sharing the same name and outcome collapse into one
 * expandable summary line (`reviewer failed × 8 in 20m`) instead of a wall
 * of identical rows — a repeated incident is one line, not eight.
 * Orphaned (daemon-restart) failures render gray/neutral, never red, and
 * never share a group with a genuine failure of the same name.
 */

import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import StatusVerdictChips from "@/components/ui/StatusVerdictChips";
import Duration from "@/components/ui/Duration";
import Skeleton from "@/components/ui/Skeleton";
import { deriveDisplayStatus } from "@/lib/runStatus";
import type { RunSummary } from "@/lib/types";
import { groupConsecutiveRecentRuns, groupSpanSec } from "./recentGroups";
import type { RecentGroup } from "./recentGroups";

interface Props {
  runs: RunSummary[];
  nowSec: number;
}

/** Placeholder row count while the first fetch is in flight. */
const SKELETON_ROWS = 4;

/** Shimmering row placeholders, sized to match a real recent-run row. */
export function RecentRunsSkeleton() {
  return (
    <div aria-hidden="true">
      <div className="mb-2 flex items-center justify-between">
        <Skeleton className="h-4 w-16" />
        <Skeleton className="h-3 w-14" />
      </div>
      <div className="overflow-hidden rounded border border-edge">
        {Array.from({ length: SKELETON_ROWS }, (_, i) => (
          <div
            key={i}
            className="flex items-center gap-3 bg-surface-raised px-3 py-1.5"
            style={{ borderTop: i === 0 ? undefined : "1px solid var(--edge-hairline)" }}
          >
            <Skeleton className="h-4 w-11 shrink-0 rounded" />
            <Skeleton className="h-3 flex-1" />
            <Skeleton className="h-3 w-10 shrink-0" />
          </div>
        ))}
      </div>
    </div>
  );
}

function durationSec(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  const end = run.ended_at ?? nowSec;
  return Math.max(0, end - run.started_at);
}

function formatSpan(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m - h * 60;
  return mm > 0 ? `${h}h ${mm}m` : `${h}h`;
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
  "orphaned",
]);

export default function RecentRuns({ runs, nowSec }: Props) {
  const t = useTranslations("mission");
  const tStatus = useTranslations("history.status");
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());

  // Localize known lifecycle statuses; unknown values fall back to the pill default
  const statusLabel = (status: string): string | undefined => {
    const s = status.toLowerCase();
    return KNOWN_STATUSES.has(s) ? tStatus(s as Parameters<typeof tStatus>[0]) : undefined;
  };

  function toggle(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  const groups = groupConsecutiveRecentRuns(runs);

  return (
    <section aria-labelledby="recent-runs-heading">
      <div className="mb-2 flex items-center justify-between">
        <SectionLabel
          trailing={
            <Link
              to="/fleet"
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
          {groups.map((group, idx) => (
            <RecentGroupRows
              key={group.key}
              group={group}
              nowSec={nowSec}
              first={idx === 0}
              expanded={expanded.has(group.key)}
              onToggle={() => toggle(group.key)}
              statusLabel={statusLabel}
              t={t}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function RunRow({
  run,
  nowSec,
  first,
  statusLabel,
}: {
  run: RunSummary;
  nowSec: number;
  first: boolean;
  statusLabel: (status: string) => string | undefined;
}) {
  const name = run.playbook_name ?? run.agent_name ?? run.run_id.slice(-12);
  const dur = durationSec(run, nowSec);
  return (
    <Link
      to="/fleet"
      search={{ s: run.run_id }}
      className="flex items-center gap-3 bg-surface-raised px-3 py-1.5 transition-colors duration-100 hover:bg-surface-overlay"
      style={{ borderTop: first ? undefined : "1px solid var(--edge-hairline)" }}
    >
      <StatusVerdictChips run={run} statusLabel={statusLabel(deriveDisplayStatus(run))} />
      <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-secondary">
        {name}
      </span>
      <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
        <Duration value={dur} />
      </span>
    </Link>
  );
}

function RecentGroupRows({
  group,
  nowSec,
  first,
  expanded,
  onToggle,
  statusLabel,
  t,
}: {
  group: RecentGroup;
  nowSec: number;
  first: boolean;
  expanded: boolean;
  onToggle: () => void;
  statusLabel: (status: string) => string | undefined;
  t: ReturnType<typeof useTranslations>;
}) {
  if (group.runs.length === 1) {
    return <RunRow run={group.runs[0]} nowSec={nowSec} first={first} statusLabel={statusLabel} />;
  }

  // Every run in a group shares one derived display status, so the newest run
  // stands in for the whole group's chip — orphaned handling lives in
  // deriveDisplayStatus, not a per-row branch.
  const lead = group.runs[0];
  const span = formatSpan(groupSpanSec(group));
  const label = statusLabel(group.displayStatus) ?? group.displayStatus;

  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 bg-surface-raised px-3 py-1.5 text-left transition-colors duration-100 hover:bg-surface-overlay"
        style={{ borderTop: first ? undefined : "1px solid var(--edge-hairline)" }}
      >
        <StatusVerdictChips run={lead} statusLabel={label} />
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-secondary">
          {t("recent.repeatedGroup", {
            name: group.name,
            status: label,
            count: group.runs.length,
            span,
          })}
        </span>
        <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
          {expanded ? t("recent.collapse") : t("recent.expand", { count: group.runs.length })}
        </span>
      </button>
      {expanded &&
        group.runs.map((run) => (
          <RunRow
            key={run.run_id}
            run={run}
            nowSec={nowSec}
            first={false}
            statusLabel={statusLabel}
          />
        ))}
    </div>
  );
}
