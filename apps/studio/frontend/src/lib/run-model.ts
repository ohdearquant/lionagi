// ADR-0093: the unified Run model. Aggregated client-side from four
// existing endpoints — no backend changes. The word "invocation" never
// surfaces past this module: a scheduler firing is just a Run with
// source="schedule", and `refs.invocation_id` is internal join plumbing a
// caller never renders.

import {
  getAdminHealth,
  getShow,
  listEngineRuns,
  listRuns,
  listScheduleRuns,
  listSchedules,
  listShows,
  type EngineRunSummary,
} from "@/lib/api";
import type { ScheduleRunSummary, ScheduleSummary, RunSummary, ShowSummary } from "@/lib/types";
import { deriveRunStatus, type DerivedStatus, type RunSource } from "@/lib/derive-run-status";

export interface RunReason {
  code?: string | null;
  summary?: string | null;
  error_detail?: string | null;
  exit_code?: number | null;
}

export interface RunRefs {
  session_id?: string;
  invocation_id?: string;
  schedule_id?: string;
  topic?: string;
}

export interface Run {
  id: string;
  source: RunSource;
  name: string;
  /** Raw backend status string, for display (StatusPill etc). */
  rawStatus: string;
  /** The single oracle's verdict — chips, projections, and the slide-over all read this. */
  status: DerivedStatus;
  isSlow: boolean;
  project: string | null;
  startedAt: number | null;
  endedAt: number | null;
  updatedAt: number | null;
  durationSeconds: number | null;
  refs: RunRefs;
  reason?: RunReason;
}

// One entry per source (plus the cross-cutting health probe) that failed to
// load — the canvas renders this as a visible "some data may be missing"
// notice instead of silently collapsing a partial result into "no runs".
export type SourceKey = RunSource | "health";
export type SourceErrors = Partial<Record<SourceKey, string>>;

export interface AggregateRunsResult {
  runs: Run[];
  sourceErrors: SourceErrors;
  // Sources withheld because they have no project column to scope by — not
  // a failure, but distinct from "no runs": the caller renders this so
  // "nothing found" and "nothing fetched under this project" don't look
  // the same to an operator.
  excludedSources: RunSource[];
}

function toEpochSeconds(value: string | number | null | undefined): number | null {
  if (value == null) return null;
  if (typeof value === "number") return value;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : Math.floor(parsed / 1000);
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// Runs a fan-out with at most `limit` requests in flight at once — the
// per-schedule and per-show detail fetches would otherwise issue one
// request per row with no ceiling.
async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let cursor = 0;
  async function worker() {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await fn(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => worker()));
  return results;
}

function fromAgentRun(run: RunSummary, now: number, unhealthy: Map<string, boolean>): Run {
  const id = run.run_id || run.id || "";
  const startedAt = run.started_at ?? null;
  const endedAt = run.ended_at ?? null;
  const processAlive = unhealthy.has(id) ? unhealthy.get(id) : true;
  const { status, isSlow, durationSeconds } = deriveRunStatus({
    source: "agent",
    rawStatus: run.status,
    startedAt,
    endedAt,
    now,
    processAlive,
  });
  const name =
    (run.show_topic || run.show_play_name
      ? [run.show_topic, run.show_play_name].filter(Boolean).join(" / ")
      : "") ||
    run.playbook_name ||
    run.agent_name ||
    run.name ||
    id.slice(-8);
  return {
    id: `agent:${id}`,
    source: "agent",
    name,
    rawStatus: run.status,
    status,
    isSlow,
    project: run.project ?? null,
    startedAt,
    endedAt,
    updatedAt: run.updated_at ?? null,
    durationSeconds,
    refs: {
      session_id: id || undefined,
      invocation_id: run.invocation_id ?? undefined,
      topic: run.show_topic ?? undefined,
    },
  };
}

// A schedule is "spent" once it can never fire again: disabled, no future
// fire queued, but it has fired at least once. Its completed runs render
// "expired" instead of "completed" — the whole schedule is retired, not
// just this one firing.
function isSpentSchedule(schedule: ScheduleSummary): boolean {
  return !schedule.enabled && schedule.next_fire_at == null && schedule.last_fired_at != null;
}

function fromScheduleRun(
  scheduleRun: ScheduleRunSummary,
  schedule: ScheduleSummary,
  now: number,
): Run {
  const startedAt = scheduleRun.fired_at ?? null;
  const endedAt = scheduleRun.ended_at ?? null;
  const { status, isSlow, durationSeconds } = deriveRunStatus({
    source: "schedule",
    rawStatus: scheduleRun.status,
    startedAt,
    endedAt,
    now,
    isSpentOneShotSchedule: isSpentSchedule(schedule),
  });
  return {
    id: `schedule:${scheduleRun.id}`,
    source: "schedule",
    name: schedule.name,
    rawStatus: scheduleRun.status,
    status,
    isSlow,
    project: schedule.project ?? null,
    startedAt,
    endedAt,
    updatedAt: endedAt ?? startedAt,
    durationSeconds,
    refs: {
      schedule_id: schedule.id,
      invocation_id: scheduleRun.invocation_id ?? undefined,
    },
    reason: {
      exit_code: scheduleRun.exit_code,
      error_detail: scheduleRun.error_detail,
    },
  };
}

function fromPlay(
  topic: string,
  play: {
    name: string;
    meta: {
      status: string;
      started_at: string;
      ended_at?: string;
      exit_code?: number;
    };
    session_id?: string | null;
  },
  now: number,
): Run {
  const startedAt = toEpochSeconds(play.meta.started_at);
  const endedAt = toEpochSeconds(play.meta.ended_at ?? null);
  const { status, isSlow, durationSeconds } = deriveRunStatus({
    source: "script",
    rawStatus: play.meta.status,
    startedAt,
    endedAt,
    now,
  });
  return {
    id: `script:${topic}:${play.name}`,
    source: "script",
    name: `${topic} / ${play.name}`,
    rawStatus: play.meta.status,
    status,
    isSlow,
    // Shows/plays carry no project association in the current data model —
    // they're global. When a project scope is active this source is
    // excluded upstream (see aggregateRuns) rather than shown unfiltered.
    project: null,
    startedAt,
    endedAt,
    updatedAt: endedAt ?? startedAt,
    durationSeconds,
    refs: {
      topic,
      session_id: play.session_id ?? undefined,
    },
    reason: {
      exit_code: play.meta.exit_code ?? null,
    },
  };
}

function fromEngineRun(er: EngineRunSummary, now: number): Run {
  const { status, isSlow, durationSeconds } = deriveRunStatus({
    source: "flow",
    rawStatus: er.status,
    startedAt: er.started_at,
    endedAt: er.ended_at,
    now,
  });
  return {
    id: `flow:${er.id}`,
    source: "flow",
    name: er.kind,
    rawStatus: er.status,
    status,
    isSlow,
    // Engine runs carry no project association in the current data model.
    // Excluded upstream when a project scope is active (see aggregateRuns).
    project: null,
    startedAt: er.started_at,
    endedAt: er.ended_at,
    updatedAt: er.ended_at ?? er.started_at,
    durationSeconds,
    refs: {
      session_id: er.session_id ?? undefined,
    },
    reason: {
      error_detail: er.error,
    },
  };
}

export interface AggregateRunsOptions {
  project?: string;
  /** Bounded per-source fetch size — never the full history at once. */
  limit?: number;
}

// Caps for sources whose list endpoints have no server-side limit (schedules,
// shows). Applied client-side, most-recently-active first, before the
// per-row detail fan-out — this is what keeps the default canvas query from
// costing one request per schedule/show in a large deployment.
const MAX_SCHEDULES = 30;
const MAX_SHOWS = 20;
const SCHEDULE_RUNS_PER_SCHEDULE = 20;
const FANOUT_CONCURRENCY = 5;

export async function aggregateRuns(opts: AggregateRunsOptions = {}): Promise<AggregateRunsResult> {
  const now = Math.floor(Date.now() / 1000);
  const limit = opts.limit ?? 300;
  const project = opts.project || undefined;
  const sourceErrors: SourceErrors = {};

  // ADR-0093: the project lens scopes Operations fully. Schedules carry a
  // project column and are filtered server-side. Shows and engine/flow runs
  // carry no project association in the current data model, so — rather
  // than show them unfiltered under an active project scope — they're
  // excluded entirely while a project is selected. They remain visible with
  // "All projects" selected, and Library is where global items stay listed
  // regardless of project scope.
  const includeGlobalSources = !project;

  const [agentResult, healthResult, schedulesResult, showsResult, engineRuns] = await Promise.all([
    listRuns({ per_page: limit, project }),
    getAdminHealth().catch((err) => {
      sourceErrors.health = errorMessage(err);
      return null;
    }),
    listSchedules({ project }).catch((err) => {
      sourceErrors.schedule = errorMessage(err);
      return { schedules: [] as ScheduleSummary[] };
    }),
    includeGlobalSources
      ? listShows().catch((err) => {
          sourceErrors.script = errorMessage(err);
          return [] as ShowSummary[];
        })
      : Promise.resolve([] as ShowSummary[]),
    includeGlobalSources
      ? listEngineRuns({ limit }).catch((err) => {
          sourceErrors.flow = errorMessage(err);
          return [] as EngineRunSummary[];
        })
      : Promise.resolve([] as EngineRunSummary[]),
  ]);

  const unhealthy = new Map<string, boolean>();
  if (healthResult) {
    for (const s of healthResult.sessions.unhealthy) {
      unhealthy.set(s.session_id, s.process_alive);
    }
  }

  const agentRuns = agentResult.runs.map((r) => fromAgentRun(r, now, unhealthy));

  const boundedSchedules = [...schedulesResult.schedules]
    .sort((a, b) => (b.last_fired_at ?? b.created_at) - (a.last_fired_at ?? a.created_at))
    .slice(0, MAX_SCHEDULES);

  let scheduleFetchFailures = 0;
  const scheduleRunLists = await mapWithConcurrency(
    boundedSchedules,
    FANOUT_CONCURRENCY,
    (schedule) =>
      listScheduleRuns(schedule.id, { limit: SCHEDULE_RUNS_PER_SCHEDULE })
        .then((res) => res.runs.map((run) => fromScheduleRun(run, schedule, now)))
        .catch(() => {
          scheduleFetchFailures++;
          return [] as Run[];
        }),
  );
  if (scheduleFetchFailures > 0) {
    sourceErrors.schedule = `${scheduleFetchFailures} schedule${scheduleFetchFailures === 1 ? "" : "s"} failed to load its runs`;
  }

  const boundedShows = [...showsResult]
    .sort((a, b) => {
      const aTime = typeof a.last_update === "number" ? a.last_update : 0;
      const bTime = typeof b.last_update === "number" ? b.last_update : 0;
      return bTime - aTime;
    })
    .slice(0, MAX_SHOWS);

  let showFetchFailures = 0;
  const showDetails = await mapWithConcurrency(boundedShows, FANOUT_CONCURRENCY, (show) =>
    getShow(show.topic).catch(() => {
      showFetchFailures++;
      return null;
    }),
  );
  if (showFetchFailures > 0) {
    sourceErrors.script = `${showFetchFailures} show${showFetchFailures === 1 ? "" : "s"} failed to load`;
  }

  const scriptRuns: Run[] = [];
  for (const detail of showDetails) {
    if (!detail) continue;
    for (const play of detail.plays) {
      scriptRuns.push(fromPlay(detail.topic, play, now));
    }
  }

  const flowRuns = engineRuns.map((er) => fromEngineRun(er, now));

  const all = [...agentRuns, ...scheduleRunLists.flat(), ...scriptRuns, ...flowRuns];
  all.sort((a, b) => (b.updatedAt ?? b.startedAt ?? 0) - (a.updatedAt ?? a.startedAt ?? 0));
  const excludedSources: RunSource[] = includeGlobalSources ? [] : ["script", "flow"];
  return { runs: all, sourceErrors, excludedSources };
}
