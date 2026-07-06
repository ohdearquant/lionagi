/**
 * Consecutive-run grouping for the Recent history strip.
 *
 * A wall of "reviewer failed" rows reads as 8 separate incidents when it
 * is really one incident that fired 8 times. Runs sharing the same name
 * and outcome, adjacent in the (already most-recent-first) list, collapse
 * into one summary line — expandable back into the individual runs.
 *
 * Pure and React-free so the grouping contract is testable without
 * rendering (matches the sparkline.ts / boardReducer.ts convention).
 */

import type { RunSummary } from "@/lib/types";
import { isOrphanedReason } from "@/lib/runStatus";

export interface RecentGroup {
  /** Stable key for React lists — the newest run's id. */
  key: string;
  /** All runs in the group, most-recent first. */
  runs: RunSummary[];
  name: string;
  /** Raw status shared by every run in the group. */
  status: string;
  /** True when every run in the group is an orphaned (phantom-reaped) failure. */
  orphaned: boolean;
}

function runName(run: RunSummary): string {
  return run.playbook_name ?? run.agent_name ?? run.run_id.slice(-12);
}

function isOrphaned(run: RunSummary): boolean {
  return isOrphanedReason(run);
}

/**
 * Groups adjacent runs sharing (name, status, orphaned-ness) into one
 * RecentGroup. Input is assumed already sorted most-recent-first (the
 * order deriveRecentRuns produces) — grouping is adjacency-only, so a
 * repeat separated by an unrelated run starts a new group rather than
 * merging non-consecutive history.
 */
export function groupConsecutiveRecentRuns(runs: RunSummary[]): RecentGroup[] {
  const groups: RecentGroup[] = [];

  for (const run of runs) {
    const name = runName(run);
    const status = run.status;
    const orphaned = isOrphaned(run);
    const last = groups[groups.length - 1];

    if (last && last.name === name && last.status === status && last.orphaned === orphaned) {
      last.runs.push(run);
    } else {
      groups.push({ key: run.run_id, runs: [run], name, status, orphaned });
    }
  }

  return groups;
}

/** Elapsed seconds between the oldest and newest run in a group. */
export function groupSpanSec(group: RecentGroup): number {
  if (group.runs.length < 2) return 0;
  const newest = group.runs[0];
  const oldest = group.runs[group.runs.length - 1];
  const newestAt = newest.ended_at ?? newest.started_at ?? 0;
  const oldestAt = oldest.ended_at ?? oldest.started_at ?? 0;
  return Math.max(0, newestAt - oldestAt);
}
