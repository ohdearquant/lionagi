// Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
// SPDX-License-Identifier: Apache-2.0
"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import Button from "@/components/Button";
import Duration from "@/components/Duration";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import { listRuns } from "@/lib/api";
import type { RunSummary } from "@/lib/types";
import { empty, errors } from "@/lib/copy";

function displayName(run: RunSummary): string {
  if (run.show_topic || run.show_play_name) {
    return [run.show_topic, run.show_play_name].filter(Boolean).join(" / ");
  }
  return run.playbook_name || run.agent_name || run.name || (run.run_id || run.id || "").slice(-8);
}

function durationSeconds(run: RunSummary, nowSec: number): number | null {
  if (run.started_at == null) return null;
  return (run.ended_at ?? nowSec) - run.started_at;
}

function phaseLabel(run: RunSummary): string | null {
  // current_phase is not in the RunSummary API response; use fallback labels.
  return run.agent_name || run.playbook_name || run.show_play_name || null;
}

function groupByProject(runs: RunSummary[]): { project: string; runs: RunSummary[] }[] {
  const map = new Map<string, RunSummary[]>();
  for (const run of runs) {
    const key = run.project ?? "Unassigned";
    const arr = map.get(key) ?? [];
    arr.push(run);
    map.set(key, arr);
  }
  // Sort groups: named projects alphabetically, "Unassigned" last.
  const sorted = Array.from(map.entries()).sort(([a], [b]) => {
    if (a === "Unassigned") return 1;
    if (b === "Unassigned") return -1;
    return a.localeCompare(b);
  });
  return sorted.map(([project, runs]) => ({ project, runs }));
}

function SkeletonRow() {
  return (
    <tr className="border-b border-edge-subtle">
      {[50, 18, 14, 14, 22].map((w, i) => (
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

function PlayfieldRow({ run, now }: { run: RunSummary; now: number }) {
  const durSec = durationSeconds(run, now);
  const phase = phaseLabel(run);
  return (
    <tr className="border-b border-edge-subtle text-content-secondary transition-colors duration-100 hover:bg-surface-overlay">
      <td className="px-3 py-2">
        <Link
          href={`/runs/${run.run_id}`}
          className="block font-medium text-content-primary transition-colors duration-100 hover:text-status-running"
        >
          {displayName(run)}
        </Link>
        {phase && <span className="font-mono text-meta text-content-muted">{phase}</span>}
      </td>
      <td className="px-3 py-2">
        <StatusPill value={run.status} kind="lifecycle" />
      </td>
      <td className="px-3 py-2 text-meta text-content-muted tabular-nums">
        {run.branch_count ?? 0}
      </td>
      <td className="px-3 py-2">
        <Duration value={durSec} />
      </td>
      <td className="px-3 py-2 text-meta text-content-muted">
        <Timestamp value={run.updated_at ?? null} />
      </td>
    </tr>
  );
}

function ProjectSection({
  project,
  runs,
  now,
}: {
  project: string;
  runs: RunSummary[];
  now: number;
}) {
  return (
    <>
      <tr className="bg-surface-overlay border-b border-edge">
        <td
          colSpan={5}
          className="px-3 py-1.5 font-medium text-meta uppercase tracking-[0.06em] text-content-muted"
        >
          {project}
          <span className="ml-2 font-mono font-normal text-[10px] text-content-muted/60">
            {runs.length} running
          </span>
        </td>
      </tr>
      {runs.map((run) => (
        <PlayfieldRow key={run.run_id} run={run} now={now} />
      ))}
    </>
  );
}

function PlayfieldPageInner() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  const [projectFilter, setProjectFilter] = useState<string | null>(null);
  const [knownProjects, setKnownProjects] = useState<string[]>([]);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const result = await listRuns({ page: 1, per_page: 5000, status: ["running"] });
        if (active) {
          setRuns(result.runs);
          setError(null);
          // Collect all distinct project names across fetches.
          const projects = new Set<string>();
          for (const r of result.runs) {
            if (r.project) projects.add(r.project);
          }
          setKnownProjects((prev) => {
            const merged = new Set([...prev, ...Array.from(projects)]);
            return merged.size === prev.length ? prev : Array.from(merged).sort();
          });
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
  }, []);

  // Clock tick for elapsed duration display.
  useEffect(() => {
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => clearInterval(tick);
  }, []);

  const filtered = projectFilter
    ? runs.filter((r) => (r.project ?? "Unassigned") === projectFilter)
    : runs;
  const groups = groupByProject(filtered);
  const total = runs.length;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Playfield"
        subtitle="All currently running plays and sessions across projects"
        density="tight"
        badges={
          !loading ? (
            <span className="text-meta text-content-muted tabular-nums">{total} running</span>
          ) : null
        }
      />

      {/* Project filter chips */}
      {knownProjects.length > 0 && (
        <div className="flex items-center gap-1 flex-wrap">
          <Button
            size="sm"
            variant="toggle"
            active={projectFilter === null}
            onClick={() => setProjectFilter(null)}
          >
            All projects
          </Button>
          {knownProjects.map((p) => (
            <Button
              key={p}
              size="sm"
              variant="toggle"
              active={projectFilter === p}
              onClick={() => setProjectFilter(projectFilter === p ? null : p)}
            >
              {p}
            </Button>
          ))}
        </div>
      )}

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded border border-edge bg-surface-raised shadow-card">
        <table aria-busy={loading} className="w-full text-left text-body">
          <thead>
            <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
              <th className="px-3 py-2.5 font-medium">Run</th>
              <th className="px-3 py-2.5 font-medium">Status</th>
              <th className="px-3 py-2.5 font-medium">Agents</th>
              <th className="px-3 py-2.5 font-medium">Elapsed</th>
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
            ) : groups.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-3 py-14 text-center text-body text-content-muted">
                  <span className="block mb-1 text-[11px]">{empty.runs}</span>
                  <span className="text-meta">No sessions are currently running.</span>
                </td>
              </tr>
            ) : (
              groups.map(({ project, runs: groupRuns }) => (
                <ProjectSection key={project} project={project} runs={groupRuns} now={now} />
              ))
            )}
          </tbody>
        </table>
      </div>
    </main>
  );
}

export default function PlayfieldPage() {
  return (
    <Suspense>
      <PlayfieldPageInner />
    </Suspense>
  );
}
