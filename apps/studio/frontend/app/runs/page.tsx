"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import PageHeader from "@/components/PageHeader";
import { listSessions } from "@/lib/api";
import type { SessionSummary } from "@/lib/api";

function formatTime(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function timeSince(ts: number): string {
  const sec = Math.round(Date.now() / 1000 - ts);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}

// Subtle skeleton row for loading state
function SkeletonRow() {
  return (
    <tr className="border-b border-edge-subtle">
      {[60, 28, 28, 52, 48].map((w, i) => (
        <td key={i} className="px-3 py-2.5">
          <div
            className="skeleton h-3 rounded"
            style={{ width: `${w}%`, maxWidth: `${w * 2}px` }}
          />
        </td>
      ))}
    </tr>
  );
}

export default function RunsPage() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const data = await listSessions();
        if (active) setSessions(data.sessions);
      } catch {
        if (active) setSessions([]);
      } finally {
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

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Runs"
        subtitle="Live and completed agent sessions"
        density="tight"
        badges={
          !loading ? (
            <span className="text-meta text-content-muted tabular-nums">
              {sessions.length} run{sessions.length !== 1 ? "s" : ""}
            </span>
          ) : null
        }
      />

      <div className="overflow-x-auto rounded border border-edge bg-surface-raised shadow-card">
        <table className="w-full text-left text-body">
          <thead>
            <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
              <th className="px-3 py-2.5 font-medium">Name</th>
              <th className="px-3 py-2.5 font-medium tabular-nums">Branches</th>
              <th className="px-3 py-2.5 font-medium tabular-nums">Messages</th>
              <th className="px-3 py-2.5 font-medium">Status</th>
              <th className="px-3 py-2.5 font-medium">Updated</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <>
                <SkeletonRow />
                <SkeletonRow />
                <SkeletonRow />
              </>
            ) : sessions.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-3 py-14 text-center text-body text-content-muted">
                  <span className="block mb-1 text-[11px]">No runs yet</span>
                  <span className="text-meta">
                    Use{" "}
                    <code className="rounded border border-edge bg-surface-overlay px-1 py-0.5 font-mono text-content-secondary">
                      li agent
                    </code>{" "}
                    or{" "}
                    <code className="rounded border border-edge bg-surface-overlay px-1 py-0.5 font-mono text-content-secondary">
                      li play
                    </code>{" "}
                    to start one.
                  </span>
                </td>
              </tr>
            ) : (
              sessions.map((s) => (
                <tr
                  key={s.id}
                  className="border-b border-edge-subtle text-content-secondary transition-colors duration-100 hover:bg-surface-overlay"
                >
                  <td className="px-3 py-2">
                    <Link
                      href={`/runs/${s.id}`}
                      className="font-medium text-content-primary transition-colors duration-100 hover:text-status-running"
                    >
                      {s.name || s.id.slice(0, 8)}
                    </Link>
                    <span className="ml-2 font-mono text-meta text-content-muted">
                      {s.id.slice(0, 8)}
                    </span>
                  </td>
                  <td className="px-3 py-2 tabular-nums">{s.branch_count}</td>
                  <td className="px-3 py-2 tabular-nums">{s.message_count}</td>
                  <td className="px-3 py-2">
                    <Badge tone={s.status === "running" ? "running" : "ok"}>{s.status}</Badge>
                  </td>
                  <td className="px-3 py-2 text-meta text-content-muted">
                    {s.status === "running" ? timeSince(s.updated_at) : formatTime(s.updated_at)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </main>
  );
}
