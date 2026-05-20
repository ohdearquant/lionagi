"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import StatCard from "@/components/StatCard";
import { API_BASE } from "@/lib/api";
import type { RunSummary, ShowSummary } from "@/lib/types";

interface Stats {
  playbooks: number;
  agents: number;
  runs: number;
  shows: number;
}

function formatTime(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

const STATUS_TONE: Record<string, "ok" | "pending" | "failed"> = {
  completed: "ok",
  running: "pending",
  failed: "failed",
  merged: "ok",
};

export default function DashboardPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [recentRuns, setRecentRuns] = useState<RunSummary[]>([]);
  const [shows, setShows] = useState<ShowSummary[]>([]);

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
        setRecentRuns((runsRes.runs ?? []).slice(0, 10));
        setShows(showsRes ?? []);
      } catch {
        /* ignore */
      }
    }

    void load();
    const interval = setInterval(load, 10000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

  const completedRuns = recentRuns.filter((r) => r.status === "completed").length;
  const failedRuns = recentRuns.filter((r) => r.status === "failed").length;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-6 px-4 py-6">
      <header className="border-b border-edge pb-4">
        <h1 className="text-xl font-semibold text-content-primary">Dashboard</h1>
        <p className="text-body text-content-muted">Orchestration overview</p>
      </header>

      {stats && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Link href="/playbooks" className="block">
            <StatCard label="Playbooks" value={stats.playbooks} />
          </Link>
          <Link href="/agents" className="block">
            <StatCard label="Agents" value={stats.agents} />
          </Link>
          <Link href="/runs" className="block">
            <StatCard
              label="Runs"
              value={stats.runs}
              delta={
                failedRuns > 0
                  ? `${failedRuns} failed`
                  : `${completedRuns} completed (recent)`
              }
              deltaTone={failedRuns > 0 ? "down" : "up"}
            />
          </Link>
          <Link href="/shows" className="block">
            <StatCard label="Shows" value={stats.shows} />
          </Link>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Recent Runs */}
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-label font-semibold text-content-primary">Recent Runs</h2>
            <Link
              href="/runs"
              className="text-meta uppercase tracking-[0.06em] text-content-muted hover:text-content-primary"
            >
              View all →
            </Link>
          </div>
          <div className="overflow-hidden rounded border border-edge bg-surface-raised">
            <table className="w-full text-left text-body">
              <thead>
                <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                  <th className="px-3 py-2 font-medium">Run</th>
                  <th className="px-3 py-2 font-medium">Kind</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Started</th>
                </tr>
              </thead>
              <tbody>
                {recentRuns.length === 0 ? (
                  <tr>
                    <td
                      colSpan={4}
                      className="px-3 py-6 text-center text-body text-content-muted"
                    >
                      No runs yet
                    </td>
                  </tr>
                ) : (
                  recentRuns.map((run) => (
                    <tr
                      key={run.run_id}
                      className="border-b border-edge-subtle hover:bg-surface-overlay"
                    >
                      <td className="px-3 py-2">
                        <Link
                          href={`/runs/${run.run_id}`}
                          className="font-mono text-body text-status-running hover:underline"
                        >
                          {run.run_id.slice(-12)}
                        </Link>
                      </td>
                      <td className="px-3 py-2">
                        <Badge tone={run.worker_name === "flow" ? "ok" : "pending"}>
                          {run.worker_name || "—"}
                        </Badge>
                      </td>
                      <td className="px-3 py-2">
                        <Badge tone={STATUS_TONE[run.status] ?? "pending"}>{run.status}</Badge>
                      </td>
                      <td className="px-3 py-2 text-meta text-content-muted">
                        {formatTime(run.started_at)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        {/* Active Shows */}
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-label font-semibold text-content-primary">Shows</h2>
            <Link
              href="/shows"
              className="text-meta uppercase tracking-[0.06em] text-content-muted hover:text-content-primary"
            >
              View all →
            </Link>
          </div>
          <div className="overflow-hidden rounded border border-edge bg-surface-raised">
            <table className="w-full text-left text-body">
              <thead>
                <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                  <th className="px-3 py-2 font-medium">Topic</th>
                  <th className="px-3 py-2 font-medium">Plays</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {shows.length === 0 ? (
                  <tr>
                    <td
                      colSpan={3}
                      className="px-3 py-6 text-center text-body text-content-muted"
                    >
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
                      <td className="px-3 py-2 text-content-secondary">{show.play_count}</td>
                      <td className="px-3 py-2">
                        <Badge tone={STATUS_TONE[show.latest_status] ?? "pending"}>
                          {show.latest_status}
                        </Badge>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </main>
  );
}
