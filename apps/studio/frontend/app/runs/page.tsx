"use client";

import Link from "next/link";
import { Fragment, Suspense, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import Button from "@/components/Button";
import Duration from "@/components/Duration";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import { listRuns } from "@/lib/api";
import type { RunListResponse } from "@/lib/api";
import type { RunSummary } from "@/lib/types";
import { empty, errors } from "@/lib/copy";

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

type ViewMode = "sessions" | "invocations";

interface InvocationGroup {
  invocation_id: string;
  sessions: RunSummary[];
}

function groupByInvocation(runs: RunSummary[]): {
  groups: InvocationGroup[];
  ungrouped: RunSummary[];
} {
  const map = new Map<string, RunSummary[]>();
  const ungrouped: RunSummary[] = [];
  for (const run of runs) {
    if (run.invocation_id) {
      const arr = map.get(run.invocation_id) ?? [];
      arr.push(run);
      map.set(run.invocation_id, arr);
    } else {
      ungrouped.push(run);
    }
  }
  const groups: InvocationGroup[] = Array.from(map.entries()).map(([invocation_id, sessions]) => ({
    invocation_id,
    sessions,
  }));
  return { groups, ungrouped };
}

// Health severity for invocation group rollup — worst-child wins.
// Ranks match staleness_check() output + ADR-0024 vocabulary expansion.
const HEALTH_RANK: Record<string, number> = {
  idle: 1,
  stale: 2,
  unresponsive: 3,
  orphaned: 4,
  zombie: 5,
};

function worstHealth(sessions: RunSummary[]): string | null {
  let worst: string | null = null;
  let worstRank = 0;
  for (const s of sessions) {
    const h = s.effective_health ?? null;
    if (h === null) continue;
    const rank = HEALTH_RANK[h] ?? 1;
    if (rank > worstRank) {
      worst = h;
      worstRank = rank;
    }
  }
  return worst;
}

function groupStatus(sessions: RunSummary[]): string {
  if (sessions.some((s) => s.status === "running")) return "running";
  if (sessions.some((s) => s.status === "failed")) return "failed";
  if (sessions.some((s) => s.status === "timed_out")) return "timed_out";
  if (sessions.some((s) => s.status === "aborted")) return "aborted";
  if (sessions.every((s) => s.status === "done" || s.status === "completed")) return "done";
  return sessions[0]?.status ?? "pending";
}

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
  count,
  active,
  onClick,
}: {
  value: string;
  count?: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      variant="toggle"
      size="sm"
      active={active}
      onClick={onClick}
      trailing={
        count !== undefined ? <span className="tabular-nums opacity-70">{count}</span> : undefined
      }
    >
      {value}
    </Button>
  );
}

function SkeletonRow() {
  return (
    <tr className="border-b border-edge-subtle">
      {[60, 28, 20, 20, 28, 52].map((w, i) => (
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

function SessionRow({
  run,
  now,
  indent = false,
}: {
  run: RunSummary;
  now: number;
  indent?: boolean;
}) {
  const prov = provenanceLabel(run);
  const durSec = durationSeconds(run, now);
  return (
    <tr className="border-b border-edge-subtle text-content-secondary transition-colors duration-100 hover:bg-surface-overlay">
      <td className={`px-3 py-2 ${indent ? "pl-8" : ""}`}>
        <Link
          href={`/runs/${run.run_id}`}
          className="block font-medium text-content-primary transition-colors duration-100 hover:text-status-running"
        >
          {displayName(run)}
        </Link>
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-meta text-content-muted">{shortRunId(run)}</span>
          {run.project && (
            <span
              className="rounded px-1 py-0.5 font-mono text-[10px] border border-edge text-content-muted"
              title={`Project: ${run.project} (${run.project_source ?? "unknown"})`}
            >
              {run.project}
            </span>
          )}
        </div>
      </td>
      <td className="px-3 py-2 text-meta text-content-muted font-mono">
        {run.model ?? <span className="text-content-muted/40">—</span>}
      </td>
      <td className="px-3 py-2">
        <StatusPill value={run.status} kind="lifecycle" />
      </td>
      <td className="px-3 py-2">
        {run.effective_health ? (
          <StatusPill value={run.effective_health} kind="neutral" tone="pending" />
        ) : (
          <span className="text-meta text-content-muted/40">—</span>
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
}

function RunsPageInner() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const page = Number(searchParams.get("page") ?? "1") || 1;
  const statuses = searchParams.getAll("status");
  const playbook = searchParams.get("playbook") ?? "";
  const project = searchParams.get("project") ?? "";
  const perPage = 20;

  const [data, setData] = useState<RunListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [playbookInput, setPlaybookInput] = useState(playbook);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  const [viewMode, setViewMode] = useState<ViewMode>("sessions");
  const [expandedInvocations, setExpandedInvocations] = useState<Set<string>>(new Set());
  const [knownProjects, setKnownProjects] = useState<string[]>([]);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- sync URL→input: no external system involved, single derived state write
  useEffect(() => setPlaybookInput(playbook), [playbook]);

  function setQuery(next: {
    page?: number;
    status?: string[];
    playbook?: string;
    project?: string;
  }) {
    const params = new URLSearchParams();
    const nextPage = next.page ?? 1;
    const nextStatuses = next.status ?? statuses;
    const nextPlaybook = next.playbook !== undefined ? next.playbook : playbook;
    const nextProject = next.project !== undefined ? next.project : project;
    if (nextPage > 1) params.set("page", String(nextPage));
    for (const s of nextStatuses) params.append("status", s);
    if (nextPlaybook) params.set("playbook", nextPlaybook);
    if (nextProject) params.set("project", nextProject);
    const qs = params.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  }

  useEffect(() => {
    let active = true;

    async function load() {
      // TODO(ADR-0020): server-side invocation pagination — paginate by invocation
      // parent (session_count aggregate) so page boundaries never split a group.
      // Until then, fetch the full dataset in invocations mode and group client-side.
      const effectivePage = viewMode === "invocations" ? 1 : page;
      const effectivePerPage = viewMode === "invocations" ? 5000 : perPage;
      try {
        const result = await listRuns({
          page: effectivePage,
          per_page: effectivePerPage,
          status: statuses.length > 0 ? statuses : undefined,
          playbook: playbook || undefined,
          project: project || undefined,
        });
        if (active) {
          setData(result);
          setError(null);
          // ADR-0026: collect distinct project names for filter chips.
          // Only update from an unfiltered fetch so we always see all projects.
          if (!project) {
            const projects = new Set<string>();
            for (const r of result.runs) {
              if (r.project) projects.add(r.project);
            }
            setKnownProjects((prev) => {
              const merged = new Set([...prev, ...Array.from(projects)]);
              return merged.size === prev.length ? prev : Array.from(merged).sort();
            });
          }
        }
      } catch {
        if (active) setError(errors.loadRuns);
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
  }, [page, statuses.join(","), playbook, project, viewMode]);

  useEffect(() => {
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => clearInterval(tick);
  }, []);

  function toggleStatus(s: string) {
    const next = statuses.includes(s) ? statuses.filter((x) => x !== s) : [...statuses, s];
    setQuery({ status: next, page: 1 });
  }

  function applyPlaybookFilter() {
    setQuery({ playbook: playbookInput, page: 1 });
  }

  function toggleExpand(id: string) {
    setExpandedInvocations((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const runs = data?.runs ?? [];
  const total = data?.total ?? 0;
  const totalPages = data?.total_pages ?? 1;

  const { groups: invocationGroups, ungrouped: ungroupedRuns } = useMemo(
    () => groupByInvocation(runs),
    [runs],
  );

  // Per-status counts derived from the currently loaded runs.
  // "completed" is the canonical DB value; "done" is the ADR-0025 backward-compat alias.
  const statusCounts = useMemo<Record<string, number>>(() => {
    const counts: Record<string, number> = {};
    for (const s of STATUS_FILTERS) counts[s] = 0;
    for (const run of runs) {
      const st = run.status;
      if (st === "completed") {
        counts["done"] = (counts["done"] ?? 0) + 1;
      } else if (st in counts) {
        counts[st] = (counts[st] ?? 0) + 1;
      }
    }
    return counts;
  }, [runs]);

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

      {/* View toggle + Filter bar */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="toggle"
              active={viewMode === "sessions"}
              onClick={() => setViewMode("sessions")}
            >
              Sessions
            </Button>
            <Button
              size="sm"
              variant="toggle"
              active={viewMode === "invocations"}
              onClick={() => setViewMode("invocations")}
            >
              Invocations
            </Button>
          </div>
          {/* ADR-0026: project filter chips */}
          {knownProjects.length > 0 && (
            <div className="flex items-center gap-1 border-l border-edge pl-2">
              <Button
                size="sm"
                variant="toggle"
                active={!project}
                onClick={() => setQuery({ project: "", page: 1 })}
              >
                All
              </Button>
              {knownProjects.map((p) => (
                <Button
                  key={p}
                  size="sm"
                  variant="toggle"
                  active={project === p}
                  onClick={() => setQuery({ project: project === p ? "" : p, page: 1 })}
                >
                  {p}
                </Button>
              ))}
            </div>
          )}
        </div>
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
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setPlaybookInput("");
                  setQuery({ playbook: "", page: 1 });
                }}
              >
                Clear
              </Button>
            )}
          </div>
          <div className="flex flex-wrap gap-1">
            {STATUS_FILTERS.map((s) => (
              <StatusFilterChip
                key={s}
                value={s}
                count={loading ? undefined : statusCounts[s]}
                active={statuses.includes(s)}
                onClick={() => toggleStatus(s)}
              />
            ))}
          </div>
        </div>
      </div>

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded border border-edge bg-surface-raised shadow-card">
        <table aria-busy={loading} className="w-full text-left text-body">
          <thead>
            <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
              <th className="px-3 py-2.5 font-medium">
                {viewMode === "invocations" ? "Invocation" : "Run"}
              </th>
              <th className="px-3 py-2.5 font-medium">Status</th>
              <th className="px-3 py-2.5 font-medium">
                {viewMode === "invocations" ? "Sessions" : "Activity"}
              </th>
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
            ) : viewMode === "invocations" ? (
              invocationGroups.length === 0 && ungroupedRuns.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-3 py-14 text-center text-body text-content-muted">
                    <span className="block mb-1 text-[11px]">{empty.runs}</span>
                    {(statuses.length > 0 || playbook) && (
                      <span className="text-meta">Try adjusting your filters.</span>
                    )}
                  </td>
                </tr>
              ) : (
                <>
                  {invocationGroups.map((group) => {
                    const isExpanded = expandedInvocations.has(group.invocation_id);
                    const st = groupStatus(group.sessions);
                    const health = worstHealth(group.sessions);
                    const latestUpdated = group.sessions.reduce<number | null>((acc, s) => {
                      const u = s.updated_at ?? null;
                      if (u === null) return acc;
                      return acc === null || u > acc ? u : acc;
                    }, null);
                    return (
                      <Fragment key={group.invocation_id}>
                        <tr
                          role="button"
                          tabIndex={0}
                          aria-expanded={isExpanded}
                          aria-label={`Invocation ${group.invocation_id.slice(-8)}, ${group.sessions.length} session${group.sessions.length !== 1 ? "s" : ""}`}
                          className="border-b border-edge-subtle bg-surface-overlay/50 text-content-primary cursor-pointer transition-colors duration-100 hover:bg-surface-overlay"
                          onClick={() => toggleExpand(group.invocation_id)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              toggleExpand(group.invocation_id);
                            }
                          }}
                        >
                          <td className="px-3 py-2">
                            <div className="flex items-center gap-1.5">
                              <span
                                aria-hidden="true"
                                className="inline-block w-3 text-center font-mono text-[10px] text-content-muted select-none"
                              >
                                {isExpanded ? "▼" : "▶"}
                              </span>
                              <span className="font-medium">
                                Invocation{" "}
                                <span className="font-mono text-meta text-content-muted">
                                  {group.invocation_id.slice(-8)}
                                </span>
                              </span>
                            </div>
                          </td>
                          <td className="px-3 py-2 text-meta text-content-muted/40">—</td>
                          <td className="px-3 py-2">
                            <StatusPill value={st} kind="lifecycle" />
                          </td>
                          <td className="px-3 py-2">
                            {health ? (
                              <StatusPill value={health} kind="neutral" tone="pending" />
                            ) : (
                              <span className="text-meta text-content-muted/40">—</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-content-secondary">
                            {group.sessions.length} session
                            {group.sessions.length !== 1 ? "s" : ""}
                          </td>
                          <td className="px-3 py-2 text-meta text-content-muted">
                            <Timestamp value={latestUpdated} />
                          </td>
                        </tr>
                        {isExpanded &&
                          group.sessions.map((run) => (
                            <SessionRow key={run.run_id} run={run} now={now} indent />
                          ))}
                      </Fragment>
                    );
                  })}
                  {ungroupedRuns.map((run) => (
                    <SessionRow key={run.run_id} run={run} now={now} />
                  ))}
                </>
              )
            ) : runs.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-3 py-14 text-center text-body text-content-muted">
                  <span className="block mb-1 text-[11px]">{empty.runs}</span>
                  {(statuses.length > 0 || playbook) && (
                    <span className="text-meta">Try adjusting your filters.</span>
                  )}
                </td>
              </tr>
            ) : (
              runs.map((run) => <SessionRow key={run.run_id} run={run} now={now} />)
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination — hidden in invocations mode (full dataset loaded client-side) */}
      {viewMode === "sessions" && (
        <div className="flex items-center justify-between text-meta text-content-muted">
          <span>
            Page {page} of {totalPages || 1} &mdash; {total} run
            {total !== 1 ? "s" : ""}
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
      )}
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
