// Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
// SPDX-License-Identifier: Apache-2.0
"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import Duration from "@/components/Duration";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import { listRuns } from "@/lib/api";
import type { RunSummary } from "@/lib/types";
import { errors } from "@/lib/copy";

// ── Status → Lane mapping ─────────────────────────────────────────────────────
//
// Canonical session statuses (lionagi/state/db.py:206):
//   running, completed, failed, timed_out, aborted, cancelled
//
// Display aliases handled by services/runs.py filter normalization:
//   done/success/finished → completed
//   prepared              → pending
//   aborted_after_finish  → aborted
//
// Observed in production data (2026-06-03, /api/runs/):
//   running: 11 | completed: 1518 | failed: 52 | timed_out: 48 | cancelled: 50
//   pending: 0  | aborted: 0
//
// Lane            Status values mapped
// QUEUED        → pending, prepared            (no rows in current data)
// IN PROGRESS   → running                      (11 rows)
// REVIEW        → (none) — play-level statuses running_complete/gated/blocked
//                  are not surfaced by /api/runs/; will populate once
//                  lifecycle-signal enrichment lands.
// DONE          → completed, done, success, finished
// FAILED        → failed, timed_out, timeout
// CANCELLED     → cancelled, canceled, aborted
//
// Drag-to-transition is NOT implemented: no status-transition endpoint exists
// on /api/runs/ (only /api/admin/transition, which is gated and terminal-only).
// Deferred to a follow-up once a general transition endpoint is added.

type LaneKey = "queued" | "running" | "review" | "done" | "failed" | "cancelled";

interface Lane {
  key: LaneKey;
  label: string;
  // Canonical + alias status strings that belong in this lane.
  statuses: string[];
  // Tailwind bg class for the lane header — uses existing status-* tokens.
  headerBg: string;
}

const LANES: Lane[] = [
  {
    key: "queued",
    label: "queued",
    statuses: ["pending", "prepared"],
    headerBg: "bg-status-warning-bg",
  },
  {
    key: "running",
    label: "running",
    statuses: ["running"],
    headerBg: "bg-status-running-bg",
  },
  {
    key: "review",
    label: "review",
    // No canonical session statuses map here; populated once lifecycle-signal
    // enrichment surfaces awaiting/gated states through the runs API.
    statuses: [],
    headerBg: "bg-status-selected-bg",
  },
  {
    key: "done",
    label: "done",
    statuses: ["completed", "done", "success", "finished"],
    headerBg: "bg-status-success-bg",
  },
  {
    key: "failed",
    label: "failed",
    statuses: ["failed", "timed_out", "timeout"],
    headerBg: "bg-status-error-bg",
  },
  {
    key: "cancelled",
    label: "cancelled",
    statuses: ["cancelled", "canceled", "aborted"],
    headerBg: "bg-surface-overlay",
  },
];

// Lookup: any status string → lane key. Statuses not in the table fall through
// to "cancelled" (neutral catch-all) rather than silently disappearing.
const STATUS_TO_LANE = new Map<string, LaneKey>(
  LANES.flatMap((lane) => lane.statuses.map((s) => [s, lane.key])),
);

function laneForStatus(status: string): LaneKey {
  return STATUS_TO_LANE.get(status) ?? "cancelled";
}

// ── Helpers (mirrored verbatim from app/runs/page.tsx) ────────────────────────

function displayName(run: RunSummary): string {
  if (run.show_topic || run.show_play_name) {
    return [run.show_topic, run.show_play_name].filter(Boolean).join(" / ");
  }
  return run.playbook_name || run.agent_name || run.name || shortRunId(run);
}

function shortRunId(run: RunSummary): string {
  return (run.run_id || run.id || "").slice(-8);
}

function durationSeconds(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  const end = run.ended_at ?? nowSec;
  return end - run.started_at;
}

// ── Card ──────────────────────────────────────────────────────────────────────

function KanbanCard({ run, now }: { run: RunSummary; now: number }) {
  const durSec = durationSeconds(run, now);
  const name = displayName(run);
  return (
    <Link
      href={`/runs/${run.run_id}`}
      className="block min-w-0 rounded border border-edge bg-surface-raised p-3 shadow-card transition-shadow duration-100 hover:shadow-card-hover focus:outline-none focus:ring-1 focus:ring-interactive-primary"
    >
      <div className="mb-1.5 truncate font-medium text-label text-content-primary" title={name}>
        {name}
      </div>

      {run.project && (
        <div className="mb-1.5">
          <span
            className="rounded border border-edge px-1 py-0.5 font-mono text-meta text-content-muted"
            title={`Project: ${run.project}${run.project_source ? ` (${run.project_source})` : ""}`}
          >
            {run.project}
          </span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        <StatusPill value={run.status} kind="lifecycle" />
        {run.branch_count != null && run.branch_count > 0 && (
          <span className="text-meta text-content-muted tabular-nums">
            {run.branch_count} agent{run.branch_count !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      <div className="mt-1.5 flex items-center gap-1.5 text-meta text-content-muted">
        <Duration value={durSec} />
        <span className="font-mono text-meta text-content-muted/60">{shortRunId(run)}</span>
      </div>
    </Link>
  );
}

// ── Lane column ───────────────────────────────────────────────────────────────

function LaneColumn({ lane, runs, now }: { lane: Lane; runs: RunSummary[]; now: number }) {
  const t = useTranslations("kanban");
  return (
    <div className="flex min-w-[210px] max-w-[260px] flex-1 flex-col rounded border border-edge">
      {/* Lane header */}
      <div className={`flex items-center justify-between rounded-t px-3 py-2 ${lane.headerBg}`}>
        <h2 className="text-label font-medium text-content-primary">
          {t(`lanes.${lane.key}` as Parameters<typeof t>[0])}
        </h2>
        <span className="ml-2 rounded-full border border-edge bg-surface-raised px-1.5 py-0.5 font-mono text-meta text-content-muted tabular-nums">
          {runs.length}
        </span>
      </div>

      {/* Cards */}
      <div
        className="flex flex-col gap-2 overflow-y-auto p-2"
        style={{ maxHeight: "calc(100vh - 200px)" }}
      >
        {runs.length === 0 ? (
          <div className="py-6 text-center text-meta text-content-muted/60">
            {lane.statuses.length === 0 ? t("empty.awaitingEnrichment") : t("empty.noRuns")}
          </div>
        ) : (
          runs.map((run) => <KanbanCard key={run.run_id} run={run} now={now} />)
        )}
      </div>
    </div>
  );
}

// ── Skeleton lane for loading state ──────────────────────────────────────────

function SkeletonLane({ lane }: { lane: Lane }) {
  const t = useTranslations("kanban");
  return (
    <div className="flex min-w-[210px] max-w-[260px] flex-1 flex-col rounded border border-edge">
      <div className={`flex items-center justify-between rounded-t px-3 py-2 ${lane.headerBg}`}>
        <h2 className="text-label font-medium text-content-primary">
          {t(`lanes.${lane.key}` as Parameters<typeof t>[0])}
        </h2>
        <span className="ml-2 rounded-full border border-edge bg-surface-raised px-1.5 py-0.5 font-mono text-meta text-content-muted">
          …
        </span>
      </div>
      <div className="flex flex-col gap-2 p-2">
        {[75, 55, 85].map((w, i) => (
          <div key={i} className="rounded border border-edge bg-surface-raised p-3 shadow-card">
            <div className="skeleton mb-2 h-3 rounded" style={{ width: `${w}%` }} />
            <div className="skeleton h-2.5 rounded" style={{ width: "40%" }} />
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function KanbanPage() {
  const t = useTranslations("kanban");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  // Mirror the 3-second polling pattern from app/runs/page.tsx, plus an
  // in-flight guard: at per_page=5000 a fetch can outlast the 3s interval,
  // and stacked overlapping requests would hammer the API.
  useEffect(() => {
    let active = true;
    let inFlight = false;

    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        // Fetch all runs without status filter to populate every lane at once.
        // per_page=5000 matches the invocations-mode fetch in runs/page.tsx.
        const result = await listRuns({ per_page: 5000 });
        if (active) {
          setRuns(result.runs);
          setTotal(result.total);
          setError(null);
        }
      } catch {
        if (active) setError(errors.loadRuns);
      } finally {
        inFlight = false;
        if (active) setLoading(false);
      }
    }

    void load();
    const interval = setInterval(load, 3000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

  // Separate 30-second tick for duration display — same pattern as runs/page.tsx.
  useEffect(() => {
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => clearInterval(tick);
  }, []);

  // Group runs into lanes (computed each render; runs is at most 5000 items).
  const laneRuns = new Map<LaneKey, RunSummary[]>(LANES.map((l) => [l.key, []]));
  for (const run of runs) {
    laneRuns.get(laneForStatus(run.status))!.push(run);
  }

  const runningCount = laneRuns.get("running")?.length ?? 0;

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title={t("title")}
        subtitle={t("subtitle")}
        density="tight"
        badges={
          !loading ? (
            <span className="text-meta text-content-muted tabular-nums">
              {total} run{total !== 1 ? "s" : ""}
              {runningCount > 0 && <> &mdash; {runningCount} running</>}
            </span>
          ) : null
        }
      />

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      )}

      <div className="flex gap-3 overflow-x-auto pb-2">
        {loading
          ? LANES.map((lane) => <SkeletonLane key={lane.key} lane={lane} />)
          : LANES.map((lane) => (
              <LaneColumn key={lane.key} lane={lane} runs={laneRuns.get(lane.key)!} now={now} />
            ))}
      </div>
    </main>
  );
}
