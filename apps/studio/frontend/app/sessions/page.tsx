"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import { listSessions } from "@/lib/api";
import type { SessionSummary } from "@/lib/api";

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

export default function SessionsPage() {
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
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-3 border-b border-edge pb-4">
        <div>
          <h1 className="text-xl font-semibold text-content-primary">Sessions</h1>
          <p className="text-sm text-content-muted">Live and completed session monitoring</p>
        </div>
        <div className="flex items-center gap-3">
          <span className="ml-auto text-xs text-content-muted">
            {sessions.length} session{sessions.length !== 1 ? "s" : ""}
          </span>
        </div>
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center py-20">
          <p className="text-sm text-content-muted">Loading sessions...</p>
        </div>
      ) : sessions.length === 0 ? (
        <div className="flex flex-1 items-center justify-center py-20">
          <p className="text-center text-sm text-content-muted">
            No sessions yet.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-edge text-xs uppercase text-content-muted">
                <th className="px-3 py-2">Name</th>
                <th className="px-3 py-2">Branches</th>
                <th className="px-3 py-2">Messages</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Updated</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id} className="border-b border-edge/50 hover:bg-surface-input/50">
                  <td className="px-3 py-2">
                    <Link
                      href={`/sessions/${s.id}`}
                      className="font-medium text-content-primary hover:text-blue-400"
                    >
                      {s.name || s.id}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-content-secondary">{s.branch_count}</td>
                  <td className="px-3 py-2 text-content-secondary">{s.message_count}</td>
                  <td className="px-3 py-2">
                    <Badge tone={s.status === "running" ? "running" : "ok"}>
                      {s.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-xs text-content-muted">
                    {formatTime(s.updated_at)}
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
