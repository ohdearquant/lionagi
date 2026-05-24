"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import Duration from "@/components/Duration";
import { listInvocations } from "@/lib/api";
import type { InvocationListResponse } from "@/lib/api";
import { empty, errors } from "@/lib/copy";

const LIMIT = 25;

function shortId(id: string): string {
  return id.slice(0, 8);
}

function durationSeconds(startedAt: number, endedAt: number | null, nowSec: number): number {
  return (endedAt ?? nowSec) - startedAt;
}

export default function InvocationsPage() {
  const [data, setData] = useState<InvocationListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [skillFilter, setSkillFilter] = useState<string>("");
  const [skillInput, setSkillInput] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  useEffect(() => {
    let active = true;
    async function load() {
      setLoading(true);
      try {
        const d = await listInvocations({
          limit: LIMIT,
          offset,
          skill: skillFilter || undefined,
          status: statusFilter || undefined,
        });
        if (active) {
          setData(d);
          setError(null);
        }
      } catch {
        if (active) setError(errors.loadInvocations);
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
  }, [offset, skillFilter, statusFilter]);

  const rows = useMemo(() => data?.invocations ?? [], [data]);

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Invocations"
        subtitle="Skill-level orchestration (ADR-0020)"
        density="tight"
        badges={
          data ? (
            <span className="text-meta text-content-muted tabular-nums">
              {rows.length} invocation{rows.length !== 1 ? "s" : ""}
            </span>
          ) : null
        }
      />

      {/* Filter strip */}
      <div className="flex flex-wrap items-center gap-3 rounded border border-edge bg-surface-overlay px-3 py-2 text-body">
        <span className="text-meta uppercase tracking-[0.06em] text-content-muted">Filters</span>
        <input
          type="text"
          placeholder="Filter by skill..."
          value={skillInput}
          onChange={(e) => setSkillInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setOffset(0);
              setSkillFilter(skillInput);
            }
          }}
          className="rounded border border-edge bg-surface-base px-2 py-1 text-body"
        />
        <Button
          size="sm"
          variant="secondary"
          onClick={() => {
            setOffset(0);
            setSkillFilter(skillInput);
          }}
        >
          Search
        </Button>
        {skillFilter && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setSkillInput("");
              setSkillFilter("");
              setOffset(0);
            }}
          >
            Clear
          </Button>
        )}
        <select
          value={statusFilter}
          onChange={(e) => {
            setOffset(0);
            setStatusFilter(e.target.value);
          }}
          className="rounded border border-edge bg-surface-base px-2 py-1 text-body"
        >
          <option value="">all statuses</option>
          <option value="running">running</option>
          <option value="completed">completed</option>
          <option value="failed">failed</option>
          <option value="timed_out">timed_out</option>
          <option value="aborted">aborted</option>
          <option value="cancelled">cancelled</option>
        </select>
      </div>

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded border border-edge bg-surface-raised shadow-card">
        <table className="w-full text-left text-body">
          <thead>
            <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
              <th className="px-3 py-2.5 font-medium">Skill</th>
              <th className="px-3 py-2.5 font-medium">Prompt</th>
              <th className="px-3 py-2.5 font-medium tabular-nums">Sessions</th>
              <th className="px-3 py-2.5 font-medium tabular-nums">Duration</th>
              <th className="px-3 py-2.5 font-medium">Status</th>
              <th className="px-3 py-2.5 font-medium">Started</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-meta text-content-muted">
                  Loading...
                </td>
              </tr>
            ) : rows.length === 0 ? (
              !error ? (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-meta text-content-muted">
                    {empty.invocations} Skills track here once they call{" "}
                    <code className="text-content-primary">li invoke start</code>.
                  </td>
                </tr>
              ) : null
            ) : (
              rows.map((inv) => {
                const dur = durationSeconds(inv.started_at, inv.ended_at, now);
                return (
                  <tr
                    key={inv.id}
                    className="border-b border-edge last:border-b-0 hover:bg-surface-overlay"
                  >
                    <td className="px-3 py-2 align-middle">
                      <Link
                        href={`/invocations/${inv.id}`}
                        className="font-mono text-content-primary hover:underline"
                      >
                        /{inv.skill}
                      </Link>
                      <div className="text-meta text-content-muted">
                        {shortId(inv.id)}
                        {inv.plugin ? ` · ${inv.plugin}` : ""}
                      </div>
                    </td>
                    <td className="px-3 py-2 align-middle">
                      <div className="max-w-md truncate text-content-secondary">
                        {inv.prompt ?? "(no prompt)"}
                      </div>
                    </td>
                    <td className="px-3 py-2 align-middle tabular-nums">{inv.session_count}</td>
                    <td className="px-3 py-2 align-middle tabular-nums">
                      <Duration value={dur} />
                    </td>
                    <td className="px-3 py-2 align-middle">
                      <StatusPill value={inv.status} />
                    </td>
                    <td className="px-3 py-2 align-middle">
                      <Timestamp value={inv.started_at} />
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between">
        <div className="text-meta text-content-muted tabular-nums">
          offset {offset} · showing {rows.length}
        </div>
        <div className="flex gap-2">
          <Button onClick={() => setOffset(Math.max(0, offset - LIMIT))} disabled={offset === 0}>
            Prev
          </Button>
          <Button onClick={() => setOffset(offset + LIMIT)} disabled={!data?.has_next}>
            Next
          </Button>
        </div>
      </div>
    </main>
  );
}
