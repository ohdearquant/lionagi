"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import MetricCard from "@/components/MetricCard";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import Duration from "@/components/Duration";
import TimeRangeChips, { type TimeRange, rangeToSeconds } from "@/components/TimeRangeChips";
import { API_BASE } from "@/lib/api";
import type { RunSummary, ShowSummary } from "@/lib/types";

interface Stats {
  playbooks: number;
  agents: number;
  runs: number;
  shows: number;
}

const RUNNING_STATES = new Set(["running", "executing", "in_progress", "director-managed", "open"]);
const FAILED_STATES = new Set(["failed", "error", "failure", "timeout", "timed_out", "cancelled", "canceled"]);
const COMPLETED_STATES = new Set([
  "completed",
  "done",
  "success",
  "finished",
  "running_complete",
  "director-managed-complete",
]);
const NEEDS_REVIEW_STATES = new Set(["needs_review", "blocked", "gated"]);

// Heuristic: a "slow" completed run is anything > 30 minutes. Tuned so the
// metric flags actual outliers, not every routine multi-minute job.
const SLOW_RUN_SECONDS = 30 * 60;
// Heuristic: a "stuck" running run hasn't finished after 30 minutes from start.
const STUCK_RUN_SECONDS = 30 * 60;

function durationSeconds(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  const end = run.finished_at ?? nowSec;
  return end - run.started_at;
}

function isInRange(run: RunSummary, windowSec: number | null, nowSec: number): boolean {
  if (!windowSec) return true;
  // a run is "in range" if it started or finished within the window
  if (run.started_at != null && nowSec - run.started_at <= windowSec) return true;
  if (run.finished_at != null && nowSec - run.finished_at <= windowSec) return true;
  return false;
}

export default function DashboardPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [allRuns, setAllRuns] = useState<RunSummary[]>([]);
  const [shows, setShows] = useState<ShowSummary[]>([]);
  const [range, setRange] = useState<TimeRange>("24h");
  const [now, setNow] = useState<number>(() => Math.floor(Date.now() / 1000));

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const [statsRes, runsRes, showsRes] = await Promise.all([
          fetch(`${API_BASE}/api/stats`).then((r) => r.json()),
          fetch(`${API_BASE}/api/runs`).then((r) => r.json()),
          fetch(`${API_BASE}/api/shows`).then((r) => r.json()),
        ]);
        if (!active) return;
        setStats(statsRes);
        setAllRuns(runsRes.runs ?? []);
        setShows(showsRes ?? []);
      } catch {
        /* ignore */
      }
    }

    void load();
    const interval = setInterval(load, 10000);
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => {
      active = false;
      clearInterval(interval);
      clearInterval(tick);
    };
  }, []);

  const windowSec = rangeToSeconds(range);

  const buckets = useMemo(() => {
    const scoped = allRuns.filter((r) => isInRange(r, windowSec, now));

    const running = scoped.filter((r) => RUNNING_STATES.has(r.status));
    const failed = scoped.filter((r) => FAILED_STATES.has(r.status));
    const completed = scoped.filter((r) => COMPLETED_STATES.has(r.status));
    const needsReview = scoped.filter((r) => NEEDS_REVIEW_STATES.has(r.status));
    const slow = scoped.filter((r) => {
      if (!COMPLETED_STATES.has(r.status)) return false;
      const d = durationSeconds(r, now);
      return d != null && d > SLOW_RUN_SECONDS;
    });
    const stuck = running.filter((r) => {
      const d = durationSeconds(r, now);
      return d != null && d > STUCK_RUN_SECONDS;
    });

    // Needs attention rows: failed / stuck / needs review / blocked
    const attention = [
      ...failed,
      ...stuck.filter((s) => !failed.includes(s)),
      ...needsReview.filter((n) => !failed.includes(n) && !stuck.includes(n)),
    ]
      .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
      .slice(0, 8);

    const recent = [...scoped]
      .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
      .slice(0, 8);

    return { scoped, running, failed, completed, needsReview, slow, stuck, attention, recent };
  }, [allRuns, windowSec, now]);

  const rangeLabel = range === "all" ? "all time" : range;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <PageHeader
        title="Dashboard"
        subtitle="Operational overview"
        actions={<TimeRangeChips value={range} onChange={setRange} />}
        density="tight"
      />

      {/* Operational stat cards */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard
          label="Running now"
          value={buckets.running.length}
          hint={buckets.stuck.length > 0 ? `${buckets.stuck.length} stuck >10m` : `${rangeLabel}`}
          tone={buckets.running.length > 0 ? "running" : "neutral"}
          icon={buckets.running.length > 0 ? "◐" : "○"}
        />
        <MetricCard
          label={`Failed (${rangeLabel})`}
          value={buckets.failed.length}
          hint={buckets.failed.length === 0 ? "all clean" : "needs investigation"}
          tone={buckets.failed.length > 0 ? "failed" : "ok"}
          icon={buckets.failed.length > 0 ? "✕" : "✓"}
        />
        <MetricCard
          label={`Slow runs (${rangeLabel})`}
          value={buckets.slow.length}
          hint={`completed > ${SLOW_RUN_SECONDS / 60}m`}
          tone={buckets.slow.length > 0 ? "pending" : "neutral"}
          icon={buckets.slow.length > 0 ? "⏱" : "·"}
        />
        <MetricCard
          label="Needs review"
          value={buckets.needsReview.length}
          hint={`${rangeLabel}`}
          tone={buckets.needsReview.length > 0 ? "pending" : "neutral"}
          icon={buckets.needsReview.length > 0 ? "⊘" : "·"}
        />
      </div>

      {/* Secondary inventory strip */}
      {stats ? (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded border border-edge bg-surface-overlay px-4 py-2 text-meta text-content-muted">
          <span className="uppercase tracking-[0.06em]">Inventory</span>
          <Link href="/playbooks" className="hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.playbooks}</span> playbooks
          </Link>
          <Link href="/agents" className="hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.agents}</span> agents
          </Link>
          <Link href="/runs" className="hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.runs}</span> runs total
          </Link>
          <Link href="/shows" className="hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.shows}</span> shows
          </Link>
        </div>
      ) : null}

      {/* Two-column: Needs attention | Recent activity */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section>
          <SectionHeader
            title="Needs attention"
            count={buckets.attention.length}
            href="/runs?filter=attention"
          />
          {buckets.attention.length === 0 ? (
            <div className="rounded border border-status-success/30 bg-status-success-bg px-4 py-4 text-body text-status-success">
              <span className="font-medium">All clean.</span>{" "}
              <span className="text-content-secondary">
                No failed, stuck, or blocked runs in window.
              </span>
            </div>
          ) : (
            <RunsTable
              runs={buckets.attention}
              emptyText=""
              now={now}
            />
          )}
        </section>

        <section>
          <SectionHeader title="Recent activity" count={buckets.recent.length} href="/runs" />
          <RunsTable runs={buckets.recent} emptyText="No runs in window." now={now} />
        </section>
      </div>

      {/* Shows table */}
      <section>
        <SectionHeader title="Shows" count={shows.length} href="/shows" />
        <div className="overflow-hidden rounded border border-edge bg-surface-raised">
          <table className="w-full text-left text-body">
            <thead>
              <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                <th className="px-3 py-2 font-medium">Topic</th>
                <th className="px-3 py-2 font-medium tabular-nums">Plays</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Last update</th>
              </tr>
            </thead>
            <tbody>
              {shows.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-6 text-center text-body text-content-muted">
                    No shows
                  </td>
                </tr>
              ) : (
                shows.map((show) => (
                  <tr
                    key={show.topic}
                    className="border-b border-edge-subtle hover:bg-surface-overlay"
                  >
                    <td className="px-3 py-2">
                      <Link
                        href={`/shows/${encodeURIComponent(show.topic)}`}
                        className="text-status-running hover:underline"
                      >
                        {show.topic}
                      </Link>
                    </td>
                    <td className="px-3 py-2 tabular-nums text-content-secondary">{show.play_count}</td>
                    <td className="px-3 py-2">
                      <StatusPill value={show.latest_status} kind="lifecycle" />
                    </td>
                    <td className="px-3 py-2 text-meta text-content-muted">
                      <Timestamp value={show.last_update ?? null} />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}

function SectionHeader({
  title,
  count,
  href,
}: {
  title: string;
  count: number;
  href: string;
}) {
  return (
    <div className="mb-3 flex items-baseline justify-between">
      <h2 className="text-label font-semibold text-content-primary">
        {title} <span className="tabular-nums text-content-muted">({count})</span>
      </h2>
      <Link
        href={href}
        className="text-meta uppercase tracking-[0.06em] text-content-muted hover:text-content-primary"
      >
        View all →
      </Link>
    </div>
  );
}

function RunsTable({
  runs,
  emptyText,
  now,
}: {
  runs: RunSummary[];
  emptyText: string;
  now: number;
}) {
  return (
    <div className="overflow-hidden rounded border border-edge bg-surface-raised">
      <table className="w-full text-left text-body">
        <thead>
          <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
            <th className="px-3 py-2 font-medium">Run</th>
            <th className="px-3 py-2 font-medium">Kind</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium tabular-nums text-right">Duration</th>
            <th className="px-3 py-2 font-medium">Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.length === 0 ? (
            <tr>
              <td colSpan={5} className="px-3 py-6 text-center text-body text-content-muted">
                {emptyText}
              </td>
            </tr>
          ) : (
            runs.map((run) => (
              <tr key={run.run_id} className="border-b border-edge-subtle hover:bg-surface-overlay">
                <td className="px-3 py-2">
                  <Link
                    href={`/runs/${run.run_id}`}
                    className="font-mono text-body text-status-running hover:underline"
                  >
                    {run.run_id.slice(-12)}
                  </Link>
                </td>
                <td className="px-3 py-2 text-content-secondary truncate max-w-[12rem]">
                  {run.worker_name || "—"}
                </td>
                <td className="px-3 py-2">
                  <StatusPill value={run.status} kind="lifecycle" />
                </td>
                <td className="px-3 py-2 tabular-nums text-right">
                  <Duration value={durationSeconds(run, now)} />
                </td>
                <td className="px-3 py-2 text-meta text-content-muted">
                  <Timestamp value={run.started_at ?? null} />
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
