// Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
// SPDX-License-Identifier: Apache-2.0
import { createFileRoute, Link, redirect } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/Button";
import Duration from "@/components/Duration";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import { listRuns } from "@/lib/api";
import { useProject } from "@/lib/project-context";
import type { RunSummary } from "@/lib/types";
import { empty, errors } from "@/lib/copy";

export const Route = createFileRoute("/playfield/")({
  beforeLoad: () => {
    throw redirect({
      to: "/",
      search: (prev) => ({ ...prev, view: "stream", live: true }),
    });
  },
  component: PlayfieldPage,
});

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
          to="/runs/$id"
          params={{ id: run.run_id }}
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
  const t = useTranslations("playfield");
  // "Unassigned" is the internal sentinel value used as a Map key and in sort
  // comparisons — translate only the display label, never the sentinel itself.
  const displayProject = project === "Unassigned" ? t("unassigned") : project;
  return (
    <>
      <tr className="bg-surface-overlay border-b border-edge">
        <td colSpan={5} className="px-3 py-1.5">
          <h2 className="inline font-medium text-meta uppercase tracking-[0.06em] text-content-muted">
            {displayProject}
          </h2>
          <span className="ml-2 font-mono font-normal text-[10px] text-content-muted/60">
            {t("groupActive", { count: runs.length })}
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
  const t = useTranslations("playfield");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  const { project: globalProject, setProject: setGlobalProject } = useProject();
  const [projectFilter, setProjectFilter] = useState<string | null>(() => globalProject || null);
  const [knownProjects, setKnownProjects] = useState<string[]>([]);

  // Follow the shared project switcher. "Unassigned" is a page-local bucket
  // with no equivalent in the shared context, so a local selection of it is
  // left alone here — only a *change* to the global scope overrides it. This
  // compares against the previous render rather than an effect (React's
  // "adjusting state" pattern) so it fires exactly on a scope change.
  const [prevGlobalProject, setPrevGlobalProject] = useState(globalProject);
  if (globalProject !== prevGlobalProject) {
    setPrevGlobalProject(globalProject);
    setProjectFilter(globalProject || null);
  }

  function selectProjectFilter(next: string | null) {
    setProjectFilter(next);
    if (next !== "Unassigned") setGlobalProject(next ?? "");
  }

  useEffect(() => {
    let active = true;
    // In-flight guard: at per_page=5000 a fetch can outlast the 3s interval,
    // and stacked overlapping requests would hammer the API.
    let inFlight = false;

    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        // "pending" expands server-side to pending+prepared — queued work an
        // operator screen should surface alongside what's already running.
        const result = await listRuns({
          page: 1,
          per_page: 5000,
          status: ["running", "pending"],
        });
        if (active) {
          setRuns(result.runs);
          setError(null);
          // Derive chips from the live result so finished projects don't
          // linger as ghost filters that select an empty table.
          const projects = new Set<string>();
          for (const r of result.runs) {
            projects.add(r.project ?? "Unassigned");
          }
          setKnownProjects(Array.from(projects).sort());
        }
      } catch {
        if (active) setError(errors.loadRuns);
      } finally {
        inFlight = false;
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
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 10000);
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
        title={t("title")}
        subtitle={t("subtitle")}
        density="tight"
        badges={
          !loading ? (
            <span className="text-meta text-content-muted tabular-nums">
              {total} {t("active")}
            </span>
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
            onClick={() => selectProjectFilter(null)}
          >
            {t("allProjects")}
          </Button>
          {knownProjects.map((p) => (
            <Button
              key={p}
              size="sm"
              variant="toggle"
              active={projectFilter === p}
              onClick={() => selectProjectFilter(projectFilter === p ? null : p)}
            >
              {/* "Unassigned" is the internal sentinel — translate display only */}
              {p === "Unassigned" ? t("unassigned") : p}
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
              <th className="px-3 py-2.5 font-medium">{t("table.run")}</th>
              <th className="px-3 py-2.5 font-medium">{t("table.status")}</th>
              <th className="px-3 py-2.5 font-medium">{t("table.agents")}</th>
              <th className="px-3 py-2.5 font-medium">{t("table.elapsed")}</th>
              <th className="px-3 py-2.5 font-medium">{t("table.updated")}</th>
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
                  <span className="text-meta">{t("empty")}</span>
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

function PlayfieldPage() {
  return <PlayfieldPageInner />;
}
