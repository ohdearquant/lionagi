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
import type { ScheduleRunSummary, ScheduleSummary, RunSummary } from "@/lib/types";
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

function toEpochSeconds(value: string | number | null | undefined): number | null {
  if (value == null) return null;
  if (typeof value === "number") return value;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : Math.floor(parsed / 1000);
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
    // they're global, so this Run is invisible to project scoping either way.
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

export async function aggregateRuns(opts: AggregateRunsOptions = {}): Promise<Run[]> {
  const now = Math.floor(Date.now() / 1000);
  const limit = opts.limit ?? 300;

  const [agentResult, healthResult, schedulesResult, showsResult, engineRuns] = await Promise.all([
    listRuns({ per_page: limit, project: opts.project || undefined }),
    getAdminHealth().catch(() => null),
    listSchedules({ project: opts.project || undefined }).catch(() => ({ schedules: [] })),
    listShows().catch(() => []),
    listEngineRuns({ limit }).catch(() => []),
  ]);

  const unhealthy = new Map<string, boolean>();
  if (healthResult) {
    for (const s of healthResult.sessions.unhealthy) {
      unhealthy.set(s.session_id, s.process_alive);
    }
  }

  const agentRuns = agentResult.runs.map((r) => fromAgentRun(r, now, unhealthy));

  const scheduleRunLists = await Promise.all(
    schedulesResult.schedules.map((schedule) =>
      listScheduleRuns(schedule.id, { limit: Math.min(limit, 100) })
        .then((res) => res.runs.map((run) => fromScheduleRun(run, schedule, now)))
        .catch(() => [] as Run[]),
    ),
  );

  const showDetails = await Promise.all(
    showsResult.map((show) => getShow(show.topic).catch(() => null)),
  );
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
  return all;
}
