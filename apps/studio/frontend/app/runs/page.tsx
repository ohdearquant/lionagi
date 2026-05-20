"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import { listRuns } from "@/lib/api";
import type { RunSummary } from "@/lib/types";

function formatDuration(started: number | null, finished: number | null): string {
  if (!started) return "—";
  const end = finished ?? Date.now() / 1000;
  const seconds = Math.round(end - started);
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}m ${secs}s`;
}

function formatTime(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

const STATUS_TONE: Record<string, "ok" | "pending" | "failed"> = {
  completed: "ok",
  running: "pending",
  failed: "failed",
};

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [workerFilter, setWorkerFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const params: Record<string, string> = {};
        if (workerFilter) params.worker = workerFilter;
        if (statusFilter) params.status = statusFilter;
        const data = await listRuns(params);
        if (active) setRuns(data.runs);
      } catch {
        if (active) setRuns([]);
      } finally {
        if (active) setLoading(false);
      }
    }

    void load();
    const interval = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [workerFilter, statusFilter]);

  const workerNames = Array.from(new Set(runs.map((r) => r.worker_name))).sort();

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-3 border-b border-edge pb-4">
        <div>
          <h1 className="text-xl font-semibold text-content-primary">Runs</h1>
          <p className="text-sm text-content-muted">Execution history across all playbooks</p>
        </div>

        <div className="flex items-center gap-3">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded border border-edge bg-surface-input px-2 py-1 text-sm text-content-secondary"
          >
            <option value="">All statuses</option>
            <option value="completed">Completed</option>
            <option value="running">Running</option>
            <option value="failed">Failed</option>
          </select>

          <select
            value={workerFilter}
            onChange={(e) => setWorkerFilter(e.target.value)}
            className="rounded border border-edge bg-surface-input px-2 py-1 text-sm text-content-secondary"
          >
            <option value="">All kinds</option>
            {workerNames.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>

          <span className="ml-auto text-xs text-content-muted">
            {runs.length} run{runs.length !== 1 ? "s" : ""}
          </span>
        </div>
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center py-20">
          <p className="text-sm text-content-muted">Loading runs...</p>
        </div>
      ) : runs.length === 0 ? (
        <div className="flex flex-1 items-center justify-center py-20">
          <p className="text-center text-sm text-content-muted">
            No runs yet. Start a run from any playbook&apos;s detail page.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-edge text-xs uppercase text-content-muted">
                <th className="px-3 py-2">Run</th>
                <th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2">Model</th>
                <th className="px-3 py-2">Task</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Steps</th>
                <th className="px-3 py-2">Duration</th>
                <th className="px-3 py-2">Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id} className="border-b border-edge/50 hover:bg-surface-input/50">
                  <td className="px-3 py-2">
                    <Link
                      href={`/runs/${run.run_id}`}
                      className="font-mono text-xs text-blue-400 hover:text-blue-300"
                    >
                      {run.run_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={run.worker_name === "flow" ? "ok" : "pending"}>
                      {run.worker_name || "—"}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-xs text-content-secondary">
                    {run.model || "—"}
                  </td>
                  <td className="max-w-sm truncate px-3 py-2 text-content-secondary" title={run.task}>
                    {run.task || "—"}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={STATUS_TONE[run.status] ?? "pending"}>{run.status}</Badge>
                  </td>
                  <td className="px-3 py-2 text-content-secondary">{run.step_count}</td>
                  <td className="px-3 py-2 font-mono text-xs text-content-secondary">
                    {formatDuration(run.started_at, run.finished_at)}
                  </td>
                  <td className="px-3 py-2 text-xs text-content-muted">
                    {formatTime(run.started_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
