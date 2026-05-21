"use client";

import Link from "next/link";
import { Suspense, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import Button from "@/components/Button";
import Duration from "@/components/Duration";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import { listRuns } from "@/lib/api";
import type { RunListResponse } from "@/lib/api";
import type { RunSummary } from "@/lib/types";

// ADR-0025: six-value session vocabulary (running/completed/failed/
// timed_out/aborted/cancelled). "done" stays as an alias for completed
// for backward-compat with show + play rows.
const STATUS_FILTERS = [
  "pending",
  "running",
  "done",
  "failed",
  "timed_out",
  "aborted",
  "cancelled",
] as const;

function displayName(run: RunSummary): string {
  if (run.show_topic || run.show_play_name) {
    return [run.show_topic, run.show_play_name].filter(Boolean).join(" / ");
  }
  return run.playbook_name || run.agent_name || run.name || shortRunId(run);
}

function shortRunId(run: RunSummary): string {
  return (run.run_id || run.id || "").slice(-8);
}

function durationSeconds(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  const end = run.ended_at ?? nowSec;
  return end - run.started_at;
}

function provenanceLabel(run: RunSummary): "fs" | "db" {
  return run.source_kind === "imported_fs" ? "fs" : "db";
}

function StatusFilterChip({
  value,
  active,
  onClick,
}: {
  value: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <Button variant="toggle" size="sm" active={active} onClick={onClick}>
      {value}
    </Button>
  );
}

function SkeletonRow() {
  return (
    <tr className="border-b border-edge-subtle">
      {[60, 28, 28, 52].map((w, i) => (
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

function RunsPageInner() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const page = Number(searchParams.get("page") ?? "1") || 1;
  const statuses = searchParams.getAll("status");
  const playbook = searchParams.get("playbook") ?? "";
  const perPage = 20;

  const [data, setData] = useState<RunListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [playbookInput, setPlaybookInput] = useState(playbook);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  // eslint-disable-next-line react-hooks/set-state-in-effect -- sync URL→input: no external system involved, single derived state write
  useEffect(() => setPlaybookInput(playbook), [playbook]);

  function setQuery(next: { page?: number; status?: string[]; playbook?: string }) {
    const params = new URLSearchParams();
    const nextPage = next.page ?? 1;
    const nextStatuses = next.status ?? statuses;
    const nextPlaybook = next.playbook !== undefined ? next.playbook : playbook;
    if (nextPage > 1) params.set("page", String(nextPage));
    for (const s of nextStatuses) params.append("status", s);
    if (nextPlaybook) params.set("playbook", nextPlaybook);
    const qs = params.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  }

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const result = await listRuns({
          page,
          per_page: perPage,
          status: statuses.length > 0 ? statuses : undefined,
          playbook: playbook || undefined,
        });
        if (active) {
          setData(result);
          setError(null);
        }
      } catch {
        if (active) setError("Failed to load runs");
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, statuses.join(","), playbook]);

  useEffect(() => {
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => clearInterval(tick);
  }, []);

  function toggleStatus(s: string) {
    const next = statuses.includes(s)
      ? statuses.filter((x) => x !== s)
      : [...statuses, s];
    setQuery({ status: next, page: 1 });
  }

  function applyPlaybookFilter() {
    setQuery({ playbook: playbookInput, page: 1 });
  }

  const runs = data?.runs ?? [];
  const total = data?.total ?? 0;
  const totalPages = data?.total_pages ?? 1;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Runs"
        subtitle="Live and completed agent sessions"
        density="tight"
        badges={
          !loading ? (
            <span className="text-meta text-content-muted tabular-nums">
              {total} run{total !== 1 ? "s" : ""}
            </span>
          ) : null
        }
      />

      {/* Filter bar */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
        <div className="flex items-center gap-1.5">
          <input
            type="text"
            placeholder="Filter by playbook..."
            value={playbookInput}
            onChange={(e) => setPlaybookInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && applyPlaybookFilter()}
            className="h-7 rounded border border-edge bg-surface-raised px-2.5 text-meta text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none"
          />
          <Button size="sm" variant="secondary" onClick={applyPlaybookFilter}>
            Search
          </Button>
          {playbook && (
            <Button size="sm" variant="ghost" onClick={() => {
              setPlaybookInput("");
              setQuery({ playbook: "", page: 1 });
            }}>
              Clear
            </Button>
          )}
        </div>
        <div className="flex flex-wrap gap-1">
          {STATUS_FILTERS.map((s) => (
            <StatusFilterChip
              key={s}
              value={s}
              active={statuses.includes(s)}
              onClick={() => toggleStatus(s)}
            />
          ))}
        </div>
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
              <th className="px-3 py-2.5 font-medium">Run</th>
              <th className="px-3 py-2.5 font-medium">Model</th>
              <th className="px-3 py-2.5 font-medium">Status</th>
              <th className="px-3 py-2.5 font-medium">Health</th>
              <th className="px-3 py-2.5 font-medium">Activity</th>
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
            ) : runs.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-3 py-14 text-center text-body text-content-muted">
                  <span className="block mb-1 text-[11px]">No runs found</span>
                  {(statuses.length > 0 || playbook) && (
                    <span className="text-meta">Try adjusting your filters.</span>
                  )}
                </td>
              </tr>
            ) : (
              runs.map((run) => {
                const prov = provenanceLabel(run);
                const durSec = durationSeconds(run, now);
                // ADR-0024 §C: "do not show a blue Running pill alone
                // when health is stale." When running + degraded health,
                // demote the status pill's emphasis so the operator's
                // eye lands on the health column instead.
                const isRunning = run.status === "running";
                const health = run.effective_health ?? null;
                const degraded =
                  isRunning &&
                  health != null &&
                  health !== "healthy" &&
                  health !== "idle";
                return (
                  <tr
                    key={run.run_id}
                    className="border-b border-edge-subtle text-content-secondary transition-colors duration-100 hover:bg-surface-overlay"
                  >
                    <td className="px-3 py-2">
                      <Link
                        href={`/runs/${run.run_id}`}
                        className="block font-medium text-content-primary transition-colors duration-100 hover:text-status-running"
                      >
                        {displayName(run)}
                      </Link>
                      <span className="font-mono text-meta text-content-muted">
                        {shortRunId(run)}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      {run.model ? (
                        <span
                          className="font-mono text-meta text-content-secondary"
                          title={run.effort ? `effort: ${run.effort}` : undefined}
                        >
                          {run.model}
                          {run.effort ? (
                            <span className="ml-1 text-content-muted">
                              · {run.effort}
                            </span>
                          ) : null}
                        </span>
                      ) : (
                        <span className="text-meta text-content-muted">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <StatusPill
                        value={run.status}
                        kind="lifecycle"
                        taxonomy="session"
                        tone={degraded ? "neutral" : undefined}
                      />
                    </td>
                    <td className="px-3 py-2">
                      {health ? (
                        <StatusPill
                          value={health}
                          kind="lifecycle"
                          taxonomy="health"
                        />
                      ) : (
                        <span className="text-meta text-content-muted">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Duration value={durSec} />
                        <span
                          className="rounded px-1 py-0.5 font-mono text-[10px] border border-edge text-content-muted"
                          title={`Source: ${run.source_kind ?? "live"}`}
                        >
                          {prov}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-meta text-content-muted">
                      <Timestamp value={run.updated_at ?? null} />
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between text-meta text-content-muted">
        <span>
          Page {page} of {totalPages || 1} &mdash; {total} run{total !== 1 ? "s" : ""}
        </span>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="secondary"
            disabled={!data?.has_prev}
            onClick={() => setQuery({ page: page - 1 })}
          >
            Previous
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={!data?.has_next}
            onClick={() => setQuery({ page: page + 1 })}
          >
            Next
          </Button>
        </div>
      </div>
    </main>
  );
}

export default function RunsPage() {
  return (
    <Suspense>
      <RunsPageInner />
    </Suspense>
  );
}
