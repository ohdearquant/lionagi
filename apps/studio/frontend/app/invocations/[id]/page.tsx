"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import Duration from "@/components/Duration";
import { getInvocation } from "@/lib/api";
import type { InvocationDetail } from "@/lib/api";

function shortId(id: string): string {
  return id.slice(0, 8);
}

export default function InvocationDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [data, setData] = useState<InvocationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const d = await getInvocation(id);
        if (active) {
          setData(d);
          setError(null);
        }
      } catch {
        if (active) setError("Failed to load invocation");
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => {
      active = false;
      clearInterval(tick);
    };
  }, [id]);

  if (loading) {
    return (
      <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
        <PageHeader title="Invocation" subtitle="loading…" density="tight" />
      </main>
    );
  }
  if (error || !data) {
    return (
      <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
        <PageHeader
          title="Invocation"
          subtitle={error ?? "Not found"}
          density="tight"
        />
      </main>
    );
  }

  const dur = (data.ended_at ?? now) - data.started_at;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title={`/${data.skill}`}
        subtitle={data.prompt ?? "(no prompt)"}
        density="tight"
        badges={<StatusPill value={data.status} />}
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 text-body">
        <div className="rounded border border-edge bg-surface-raised p-3">
          <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
            Sessions
          </div>
          <div className="mt-1 text-2xl tabular-nums text-content-primary">
            {data.session_count}
          </div>
        </div>
        <div className="rounded border border-edge bg-surface-raised p-3">
          <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
            Duration
          </div>
          <div className="mt-1 text-2xl tabular-nums text-content-primary">
            <Duration value={dur} />
          </div>
        </div>
        <div className="rounded border border-edge bg-surface-raised p-3">
          <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
            Started
          </div>
          <div className="mt-1 text-body text-content-primary">
            <Timestamp value={data.started_at} />
          </div>
        </div>
        <div className="rounded border border-edge bg-surface-raised p-3">
          <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
            Plugin
          </div>
          <div className="mt-1 text-body text-content-primary">
            {data.plugin ?? "—"}
          </div>
        </div>
      </div>

      <section>
        <h2 className="mb-2 text-meta uppercase tracking-[0.06em] text-content-muted">
          Sessions in this invocation
        </h2>
        <div className="overflow-x-auto rounded border border-edge bg-surface-raised shadow-card">
          <table className="w-full text-left text-body">
            <thead>
              <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                <th className="px-3 py-2.5 font-medium">Session</th>
                <th className="px-3 py-2.5 font-medium">Kind</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 font-medium">Started</th>
                <th className="px-3 py-2.5 font-medium">Last activity</th>
              </tr>
            </thead>
            <tbody>
              {data.sessions.length === 0 ? (
                <tr>
                  <td
                    colSpan={5}
                    className="px-3 py-8 text-center text-meta text-content-muted"
                  >
                    No sessions spawned under this invocation yet.
                  </td>
                </tr>
              ) : (
                data.sessions.map((s) => (
                  <tr
                    key={s.id}
                    className="border-b border-edge last:border-b-0 hover:bg-surface-overlay"
                  >
                    <td className="px-3 py-2 align-middle">
                      <Link
                        href={`/runs/${s.id}`}
                        className="font-mono text-content-primary hover:underline"
                      >
                        {s.name || s.agent_name || s.playbook_name || shortId(s.id)}
                      </Link>
                      <div className="text-meta text-content-muted font-mono">
                        {shortId(s.id)}
                      </div>
                    </td>
                    <td className="px-3 py-2 align-middle text-content-secondary">
                      {s.invocation_kind ?? "—"}
                    </td>
                    <td className="px-3 py-2 align-middle">
                      <StatusPill value={s.status ?? "pending"} />
                    </td>
                    <td className="px-3 py-2 align-middle">
                      <Timestamp value={s.started_at} />
                    </td>
                    <td className="px-3 py-2 align-middle">
                      <Timestamp value={s.last_message_at} />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {data.node_metadata && Object.keys(data.node_metadata).length > 0 ? (
        <section>
          <h2 className="mb-2 text-meta uppercase tracking-[0.06em] text-content-muted">
            Skill metadata
          </h2>
          <pre className="overflow-x-auto rounded border border-edge bg-surface-raised p-3 text-meta font-mono text-content-secondary">
            {JSON.stringify(data.node_metadata, null, 2)}
          </pre>
        </section>
      ) : null}
    </main>
  );
}
