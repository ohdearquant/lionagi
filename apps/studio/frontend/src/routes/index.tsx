import { createFileRoute, Link } from "@tanstack/react-router";
import type { LinkProps } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import MetricCard from "@/components/MetricCard";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import Duration from "@/components/Duration";
import TimeRangeChips, { type TimeRange, rangeToSeconds } from "@/components/TimeRangeChips";
import { getShow, getStats, listRuns, listShows } from "@/lib/api";
import type { DbStats, StudioStats } from "@/lib/api";
import type { RunSummary, ShowSummary } from "@/lib/types";

export const Route = createFileRoute("/")({
  component: DashboardPage,
});

const RUNNING_STATES = new Set(["running", "executing", "in_progress", "director-managed", "open"]);
// timed_out / aborted / cancelled are NOT failures — deliberate bound or system cancellation.
const FAILED_STATES = new Set(["failed", "error", "failure"]);
const COMPLETED_STATES = new Set([
  "completed",
  "done",
  "success",
  "finished",
  "running_complete",
  "director-managed-complete",
]);
const NEEDS_REVIEW_STATES = new Set(["needs_review", "blocked", "gated"]);

// Thresholds: typical runs are 45-90 min; 30 min would flag nearly every flow.
const SLOW_RUN_SECONDS = 60 * 60;
const STUCK_RUN_SECONDS = 60 * 60;

function durationSeconds(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  const end = run.ended_at ?? nowSec;
  return end - run.started_at;
}

function isInRange(run: RunSummary, windowSec: number | null, nowSec: number): boolean {
  if (!windowSec) return true;
  if (run.started_at != null && nowSec - run.started_at <= windowSec) return true;
  if (run.ended_at != null && nowSec - run.ended_at <= windowSec) return true;
  return false;
}

function DashboardPage() {
  const t = useTranslations("dashboard");
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
        const [statsRes, runsRes, showsList] = await Promise.all([
          getStats(),
          listRuns({ per_page: 2000 }),
          listShows(),
        ]);
        if (!active) return;
        setStats(statsRes);
        setAllRuns(runsRes.runs ?? []);
        setShows(showsList);
        setFetchError(null);
        if (showsList.length > 0) {
          const settled = await Promise.allSettled(
            showsList.map((s: ShowSummary) =>
              getShow(s.topic).then((d) => ({
                topic: s.topic,
                status: d.status ?? s.latest_status,
              })),
            ),
          );
          if (active) {
            const resolved = new Map<string, string>();
            for (const r of settled) {
              if (r.status === "fulfilled") resolved.set(r.value.topic, r.value.status);
            }
            if (resolved.size > 0) {
              setShows((prev) =>
                prev.map((s) =>
                  resolved.has(s.topic) ? { ...s, latest_status: resolved.get(s.topic)! } : s,
                ),
              );
            }
          }
        }
      } catch {
        if (active) setFetchError(t("fetchError"));
      }
    }

    void load();
    const interval = setInterval(load, 30000);
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => {
      active = false;
      clearInterval(interval);
      clearInterval(tick);
    };
  }, [t]);

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
    const stale = running.filter((r) => r.effective_health === "stale");
    const attention = [
      ...failed,
      ...stale.filter((s) => !failed.includes(s)),
      ...stuck.filter((s) => !failed.includes(s) && !stale.includes(s)),
      ...needsReview.filter((n) => !failed.includes(n) && !stale.includes(n) && !stuck.includes(n)),
    ]
      .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
      .slice(0, 8);
    const recent = [...scoped]
      .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
      .slice(0, 8);
    return {
      scoped,
      running,
      failed,
      completed,
      needsReview,
      slow,
      stuck,
      stale,
      attention,
      recent,
    };
  }, [allRuns, windowSec, now]);

  const rangeLabel = range === "all" ? t("metrics.allTime") : range;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title={t("title")}
        subtitle={t("subtitle")}
        actions={<TimeRangeChips value={range} onChange={setRange} />}
        density="tight"
      />

      {fetchError && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {fetchError}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <MetricCard
          label={t("metrics.runningNow")}
          value={buckets.running.length}
          hint={
            buckets.stuck.length > 0
              ? t("metrics.stuck", { count: buckets.stuck.length, minutes: STUCK_RUN_SECONDS / 60 })
              : rangeLabel
          }
          tone={buckets.running.length > 0 ? "running" : "neutral"}
          icon={buckets.running.length > 0 ? "◐" : "○"}
        />
        <MetricCard
          label={t("metrics.stale")}
          value={buckets.stale.length}
          hint={
            buckets.stale.length > 0
              ? t("metrics.noActivityPastThreshold")
              : t("metrics.noStaleActivity")
          }
          tone={buckets.stale.length > 0 ? "pending" : "neutral"}
          icon={buckets.stale.length > 0 ? "◴" : "·"}
        />
        <MetricCard
          label={t("metrics.failed", { range: rangeLabel })}
          value={buckets.failed.length}
          hint={
            buckets.failed.length === 0 ? t("metrics.allClean") : t("metrics.needsInvestigation")
          }
          tone={buckets.failed.length > 0 ? "failed" : "ok"}
          icon={buckets.failed.length > 0 ? "✕" : "✓"}
        />
        <MetricCard
          label={t("metrics.slowRuns", { range: rangeLabel })}
          value={buckets.slow.length}
          hint={t("metrics.completedOver", { minutes: SLOW_RUN_SECONDS / 60 })}
          tone={buckets.slow.length > 0 ? "pending" : "neutral"}
          icon={buckets.slow.length > 0 ? "⏱" : "·"}
        />
        <MetricCard
          label={t("metrics.needsReview")}
          value={buckets.needsReview.length}
          hint={rangeLabel}
          tone={buckets.needsReview.length > 0 ? "pending" : "neutral"}
          icon={buckets.needsReview.length > 0 ? "⊘" : "·"}
        />
      </div>

      {stats ? (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded border border-edge bg-surface-overlay px-4 py-2.5 text-meta text-content-muted">
          <span className="uppercase tracking-[0.08em] text-content-muted">
            {t("inventory.label")}
          </span>
          <Link
            to="/playbooks"
            className="transition-colors duration-100 hover:text-content-primary"
          >
            <span className="tabular-nums text-content-secondary">{stats.playbooks}</span>{" "}
            {t("inventory.playbooks")}
          </Link>
          <Link to="/agents" className="transition-colors duration-100 hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.agents}</span>{" "}
            {t("inventory.agents")}
          </Link>
          <Link to="/runs" className="transition-colors duration-100 hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.runs}</span>{" "}
            {t("inventory.runsTotal")}
          </Link>
          <Link to="/shows" className="transition-colors duration-100 hover:text-content-primary">
            <span className="tabular-nums text-content-secondary">{stats.shows}</span>{" "}
            {t("inventory.shows")}
          </Link>
        </div>
      ) : null}

      <SystemHealthCard db={stats?.db} />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section>
          <SectionHeader
            title={t("sections.needsAttention")}
            count={buckets.attention.length}
            href="/runs"
          />
          {buckets.attention.length === 0 ? (
            <div className="rounded border border-status-success/25 bg-status-success-bg px-4 py-4 text-body text-status-success shadow-card">
              <span className="font-semibold">{t("allClean")}</span>{" "}
              <span className="text-content-secondary">{t("allCleanDetail")}</span>
            </div>
          ) : (
            <RunsTable runs={buckets.attention} emptyText="" now={now} />
          )}
        </section>
        <section>
          <SectionHeader
            title={t("sections.recentActivity")}
            count={buckets.recent.length}
            href="/runs"
          />
          <RunsTable runs={buckets.recent} emptyText="No runs in window." now={now} />
        </section>
      </div>

      <section>
        <SectionHeader title={t("sections.shows")} count={shows.length} href="/shows" />
        <div className="overflow-hidden rounded border border-edge bg-surface-raised shadow-card">
          <table className="w-full text-left text-body">
            <thead>
              <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                <th className="px-3 py-2.5 font-medium">{t("table.topic")}</th>
                <th className="px-3 py-2.5 font-medium tabular-nums">{t("table.plays")}</th>
                <th className="px-3 py-2.5 font-medium">{t("table.status")}</th>
                <th className="px-3 py-2.5 font-medium">{t("table.lastUpdate")}</th>
              </tr>
            </thead>
            <tbody>
              {shows.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-8 text-center text-meta text-content-muted">
                    {t("table.noShows")}
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
                        to="/shows/$topic"
                        params={{ topic: show.topic }}
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

function SectionHeader({
  title,
  count,
  href,
}: {
  title: string;
  count: number;
  href: LinkProps["to"];
}) {
  const t = useTranslations("dashboard");
  return (
    <div className="mb-2.5 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <h2 className="text-label font-semibold text-content-primary">{title}</h2>
        <span className="rounded bg-surface-overlay px-1.5 py-0.5 font-mono text-meta tabular-nums text-content-muted">
          {count}
        </span>
      </div>
      <Link
        to={href}
        className="text-meta text-content-muted transition-colors duration-100 hover:text-content-primary"
      >
        {t("viewAll")}
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
  const t = useTranslations("dashboard");
  return (
    <div className="overflow-hidden rounded border border-edge bg-surface-raised shadow-card">
      <table className="w-full text-left text-body">
        <thead>
          <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
            <th className="px-3 py-2.5 font-medium">{t("table.run")}</th>
            <th className="px-3 py-2.5 font-medium">{t("table.kind")}</th>
            <th className="px-3 py-2.5 font-medium">{t("table.status")}</th>
            <th className="px-3 py-2.5 font-medium tabular-nums text-right">
              {t("table.duration")}
            </th>
            <th className="px-3 py-2.5 font-medium">{t("table.started")}</th>
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
                    to="/runs/$id"
                    params={{ id: run.run_id }}
                    className="font-mono text-body text-status-running transition-colors duration-100 hover:opacity-80"
                  >
                    {run.run_id.slice(-12)}
                  </Link>
                </td>
                <td className="px-3 py-2 text-content-secondary truncate max-w-[12rem]">
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
  const t = useTranslations("dashboard");
  if (!db) return null;
  return (
    <div className="rounded border border-edge bg-surface-raised p-4 shadow-card">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-label font-semibold text-content-primary">{t("health.title")}</span>
        <span className="font-mono text-meta text-content-muted">{db.path}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 text-meta">
        <div>
          <div className="text-content-muted">{t("health.stateDb")}</div>
          <div className="tabular-nums text-content-secondary">{formatBytes(db.size_bytes)}</div>
        </div>
        <div>
          <div className="text-content-muted">{t("health.wal")}</div>
          <div className="tabular-nums text-content-secondary">{formatBytes(db.wal_bytes)}</div>
        </div>
        <div>
          <div className="text-content-muted">{t("health.connections")}</div>
          <div className="tabular-nums text-content-secondary">{db.connections_active}</div>
        </div>
        <div>
          <div className="text-content-muted">{t("health.lastCheckpoint")}</div>
          <div className="text-content-secondary">
            {db.last_checkpoint_at ? (
              <Timestamp value={db.last_checkpoint_at} />
            ) : (
              t("health.unavailable")
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
            <Link to="/admin" className="text-meta text-status-running hover:underline">
              {t("health.doctor")}
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
