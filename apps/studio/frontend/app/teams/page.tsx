"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import { listTeams } from "@/lib/api";
import type { TeamListResponse, TeamSummary } from "@/lib/api";

const LIMIT = 20;

export default function TeamsPage() {
  const router = useRouter();
  const [data, setData] = useState<TeamListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    let active = true;
    async function load() {
      setLoading(true);
      try {
        const d = await listTeams({ limit: LIMIT, offset });
        if (active) {
          setData(d);
          setError(null);
        }
      } catch {
        if (active) setError("Failed to load teams");
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [offset]);

  const teams = data?.teams ?? [];

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Teams"
        subtitle="Read-only team coordination logs"
        density="tight"
        badges={
          data ? (
            <span className="text-meta text-content-muted tabular-nums">
              {data.total} team{data.total !== 1 ? "s" : ""}
            </span>
          ) : null
        }
      />

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded border border-edge bg-surface-raised shadow-card">
        <table className="w-full text-left text-body">
          <thead>
            <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
              <th className="px-3 py-2.5 font-medium">Team</th>
              <th className="px-3 py-2.5 font-medium tabular-nums">Members</th>
              <th className="px-3 py-2.5 font-medium">Last modified</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={3} className="px-3 py-8 text-center text-meta text-content-muted">
                  Loading...
                </td>
              </tr>
            ) : teams.length === 0 ? (
              <tr>
                <td colSpan={3} className="px-3 py-8 text-center text-meta text-content-muted">
                  No teams found.
                </td>
              </tr>
            ) : (
              teams.map((team) => (
                <tr
                  key={team.id}
                  tabIndex={0}
                  role="link"
                  className="cursor-pointer border-b border-edge-subtle text-content-secondary transition-colors duration-100 hover:bg-surface-overlay focus:outline-none focus:ring-1 focus:ring-inset focus:ring-interactive-primary"
                  onClick={() => router.push(`/teams/${encodeURIComponent(team.id)}`)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      router.push(`/teams/${encodeURIComponent(team.id)}`);
                    }
                  }}
                >
                  <td className="px-3 py-2">
                    <div className="font-medium text-content-primary">{team.name}</div>
                    <div className="font-mono text-meta text-content-muted">{team.id}</div>
                  </td>
                  <td className="px-3 py-2 tabular-nums">{team.member_count}</td>
                  <td className="px-3 py-2 text-meta text-content-muted">
                    <Timestamp value={team.last_modified} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-meta text-content-muted">
        <span>
          {data?.total ?? 0} team{(data?.total ?? 0) !== 1 ? "s" : ""}
        </span>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="secondary"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - LIMIT))}
          >
            Previous
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={!data?.has_next}
            onClick={() => setOffset(offset + LIMIT)}
          >
            Next
          </Button>
        </div>
      </div>
    </main>
  );
}
