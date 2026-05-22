"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import MetricCard from "@/components/MetricCard";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import Duration from "@/components/Duration";
import TimeRangeChips, { type TimeRange, rangeToSeconds } from "@/components/TimeRangeChips";
import { API_BASE, getStats } from "@/lib/api";
import type { DbStats, StudioStats } from "@/lib/api";
import type { RunSummary, ShowSummary } from "@/lib/types";

const RUNNING_STATES = new Set(["running", "executing", "in_progress", "director-managed", "open"]);
// ADR-0025: timed_out / aborted / cancelled are NOT failures. Timeout is
// a deliberate bound (amber, retry); aborted = Ctrl-C, cancelled = system
// cancellation (both neutral). Keeping them out of FAILED_STATES so the
// "Failed" dashboard card only counts actual errors.
const FAILED_STATES = new Set([
  "failed",
  "error",
  "failure",
]);
const COMPLETED_STATES = new Set([
  "completed",
  "done",
  "success",
  "finished",
  "running_complete",
  "director-managed-complete",
]);
const NEEDS_REVIEW_STATES = new Set(["needs_review", "blocked", "gated"]);

// ADR-0025: thresholds tuned for actual operational reality. Typical
// `li play` runs are 45-90 min; 30 min flagged nearly every flow as
// "slow" — useless signal. 60 min flags only the real outliers.
const SLOW_RUN_SECONDS = 60 * 60;
// A "stuck" running run hasn't finished after 60 minutes from start.
const STUCK_RUN_SECONDS = 60 * 60;

function durationSeconds(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  // H-FE-3: use ended_at (SQLite field) instead of stale finished_at
  const end = run.ended_at ?? nowSec;
  return end - run.started_at;
}

function isInRange(run: RunSummary, windowSec: number | null, nowSec: number): boolean {
  if (!windowSec) return true;
  // a run is "in range" if it started or ended within the window
  if (run.started_at != null && nowSec - run.started_at <= windowSec) return true;
  // H-FE-3: use ended_at (SQLite field) instead of stale finished_at
  if (run.ended_at != null && nowSec - run.ended_at <= windowSec) return true;
  return false;
}

export default function DashboardPage() {
  const [stats, setStats] = useState<StudioStats | null>(null);
  const [allRuns, setAllRuns] = useState<RunSummary[]>([]);
  const [shows, setShows] = useState<ShowSummary[]>([]);
  const [range, setRange] = useState<TimeRange>("24h");
  const [now, setNow] = useState<number>(() => Math.floor(Date.now() / 1000));
  const [fetchError, setFetchError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const [statsRes, runsRes, showsRes] = await Promise.all([
          getStats(),
          fetch(`${API_BASE}/api/runs?per_page=2000`).then((r) => r.json()),
          fetch(`${API_BASE}/api/shows`).then((r) => r.json()),
        ]);
        if (!active) return;
        setStats(statsRes);
        setAllRuns(runsRes.runs ?? []);
        setShows(showsRes ?? []);
        setFetchError(null);
      } catch {
        if (active) setFetchError("API unreachable — data may be stale");
      }
    }

    void load();
    const interval = setInterval(load, 30000); // ADR-0006: 30s interval
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
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Dashboard"
        subtitle="Operational overview"
        actions={<TimeRangeChips value={range} onChange={setRange} />}
        density="tight"
      />

      {fetchError && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {fetchError}
        </div>
      )}

      {/* Operational stat cards */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard
          label="Running now"
          value={buckets.running.length}
          hint={buckets.stuck.length > 0 ? `${buckets.stuck.length} stuck >${STUCK_RUN_SECONDS / 60}m` : `${rangeLabel}`}
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
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded border border-edge bg-surface-overlay px-4 py-2.5 text-meta text-content-muted">
          <span className="uppercase tracking-[0.08em] text-content-muted">Inventory</span>
          <Link
            href="/playbooks"
            className="transition-colors duration-100 hover:text-content-primary"
          >
            <span className="tabular-nums text-content-secondary">{stats.playbooks}</span> playbooks
          </Link>
          <Link
            href="/agents"
            className="transition-colors duration-100 hover:text-content-primary"
          >
            <span className="tabular-nums text-content-secondary">{stats.agents}</span> agents
          </Link>
          <Link href="/runs" className="transition-colors duration-100 hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.runs}</span> runs total
          </Link>
          <Link href="/shows" className="transition-colors duration-100 hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.shows}</span> shows
          </Link>
        </div>
      ) : null}

      <SystemHealthCard db={stats?.db} />

      {/* Two-column: Needs attention | Recent activity */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section>
          <SectionHeader
            title="Needs attention"
            count={buckets.attention.length}
            href="/runs"
          />
          {buckets.attention.length === 0 ? (
            <div className="rounded border border-status-success/25 bg-status-success-bg px-4 py-4 text-body text-status-success shadow-card">
              <span className="font-semibold">All clean.</span>{" "}
              <span className="text-content-secondary">
                No failed, stuck, or blocked runs in window.
              </span>
            </div>
          ) : (
            <RunsTable runs={buckets.attention} emptyText="" now={now} />
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
        <div className="overflow-hidden rounded border border-edge bg-surface-raised shadow-card">
          <table className="w-full text-left text-body">
            <thead>
              <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                <th className="px-3 py-2.5 font-medium">Topic</th>
                <th className="px-3 py-2.5 font-medium tabular-nums">Plays</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 font-medium">Last update</th>
              </tr>
            </thead>
            <tbody>
              {shows.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-8 text-center text-meta text-content-muted">
                    No shows
                  </td>
                </tr>
              ) : (
                shows.map((show) => (
                  <tr
                    key={show.topic}
                    className="border-b border-edge-subtle text-content-secondary transition-colors duration-100 hover:bg-surface-overlay"
                  >
                    <td className="px-3 py-2">
                      <Link
                        href={`/shows/${encodeURIComponent(show.topic)}`}
                        className="text-status-running transition-colors duration-100 hover:opacity-80"
                      >
                        {show.topic}
                      </Link>
                    </td>
                    <td className="px-3 py-2 tabular-nums">{show.play_count}</td>
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

function SectionHeader({ title, count, href }: { title: string; count: number; href: string }) {
  return (
    <div className="mb-2.5 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <h2 className="text-label font-semibold text-content-primary">{title}</h2>
        <span className="rounded bg-surface-overlay px-1.5 py-0.5 font-mono text-meta tabular-nums text-content-muted">
          {count}
        </span>
      </div>
      <Link
        href={href}
        className="text-meta text-content-muted transition-colors duration-100 hover:text-content-primary"
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
    <div className="overflow-hidden rounded border border-edge bg-surface-raised shadow-card">
      <table className="w-full text-left text-body">
        <thead>
          <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
            <th className="px-3 py-2.5 font-medium">Run</th>
            <th className="px-3 py-2.5 font-medium">Kind</th>
            <th className="px-3 py-2.5 font-medium">Status</th>
            <th className="px-3 py-2.5 font-medium tabular-nums text-right">Duration</th>
            <th className="px-3 py-2.5 font-medium">Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.length === 0 ? (
            <tr>
              <td colSpan={5} className="px-3 py-8 text-center text-meta text-content-muted">
                {emptyText}
              </td>
            </tr>
          ) : (
            runs.map((run) => (
              <tr
                key={run.run_id}
                className="border-b border-edge-subtle text-content-secondary transition-colors duration-100 hover:bg-surface-overlay"
              >
                <td className="px-3 py-2">
                  <Link
                    href={`/runs/${run.run_id}`}
                    className="font-mono text-body text-status-running transition-colors duration-100 hover:opacity-80"
                  >
                    {run.run_id.slice(-12)}
                  </Link>
                </td>
                <td className="px-3 py-2 text-content-secondary truncate max-w-[12rem]">
                  {/* H-FE-3: playbook_name replaces stale worker_name */}
                  {run.playbook_name || run.agent_name || "—"}
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

function formatBytes(value: number | null | undefined): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(value) / Math.log(1024));
  return `${(value / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function SystemHealthCard({ db }: { db: DbStats | undefined }) {
  if (!db) return null;
  return (
    <div className="rounded border border-edge bg-surface-raised p-4 shadow-card">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-label font-semibold text-content-primary">System Health</span>
        <span className="font-mono text-meta text-content-muted">{db.path}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 text-meta">
        <div>
          <div className="text-content-muted">State DB</div>
          <div className="tabular-nums text-content-secondary">{formatBytes(db.size_bytes)}</div>
        </div>
        <div>
          <div className="text-content-muted">WAL</div>
          <div className="tabular-nums text-content-secondary">{formatBytes(db.wal_bytes)}</div>
        </div>
        <div>
          <div className="text-content-muted">Connections</div>
          <div className="tabular-nums text-content-secondary">{db.connections_active}</div>
        </div>
        <div>
          <div className="text-content-muted">Last checkpoint</div>
          <div className="text-content-secondary">
            {db.last_checkpoint_at ? (
              <Timestamp value={db.last_checkpoint_at} />
            ) : (
              "unavailable"
            )}
          </div>
        </div>
      </div>
      {db.sessions_by_status && Object.keys(db.sessions_by_status).length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {Object.entries(db.sessions_by_status).map(([s, n]) => (
            <span key={s} className="flex items-center gap-1">
              <StatusPill value={s} kind="lifecycle" />
              <span className="tabular-nums text-meta text-content-muted">{n}</span>
            </span>
          ))}
          {(db.sessions_by_status["running"] ?? 0) > 0 && (
            <Link href="/admin" className="text-meta text-status-running hover:underline">
              Doctor →
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
