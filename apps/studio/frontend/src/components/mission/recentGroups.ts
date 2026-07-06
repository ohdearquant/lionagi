/**
 * Consecutive-run grouping for the Recent history strip.
 *
 * A wall of "reviewer failed" rows reads as 8 separate incidents when it
 * is really one incident that fired 8 times. Runs sharing the same name
 * and the same displayed outcome, adjacent in the (already most-recent-first)
 * list, collapse into one summary line — expandable back into the individual
 * runs. "Outcome" is the §0 keystone's derived display status, so orphaned
 * (phantom-reaped) runs group apart from genuine failures, and raw-status
 * aliases that display identically (e.g. timed_out / aborted → cancelled)
 * group together.
 *
 * Pure and React-free so the grouping contract is testable without
 * rendering (matches the sparkline.ts / boardReducer.ts convention).
 */

import type { RunSummary } from "@/lib/types";
import { deriveDisplayStatus, type DisplayStatus } from "@/lib/runStatus";

export interface RecentGroup {
  /** Stable key for React lists — the newest run's id. */
  key: string;
  /** All runs in the group, most-recent first. */
  runs: RunSummary[];
  name: string;
  /** Shared §0 display status for every run in the group. */
  displayStatus: DisplayStatus;
}

function runName(run: RunSummary): string {
  return run.playbook_name ?? run.agent_name ?? run.run_id.slice(-12);
}

/**
 * Groups adjacent runs sharing (name, displayStatus) into one RecentGroup.
 * Input is assumed already sorted most-recent-first (the order
 * deriveRecentRuns produces) — grouping is adjacency-only, so a repeat
 * separated by an unrelated run starts a new group rather than merging
 * non-consecutive history.
 */
export function groupConsecutiveRecentRuns(runs: RunSummary[]): RecentGroup[] {
  const groups: RecentGroup[] = [];

  for (const run of runs) {
    const name = runName(run);
    const displayStatus = deriveDisplayStatus(run);
    const last = groups[groups.length - 1];

    if (last && last.name === name && last.displayStatus === displayStatus) {
      last.runs.push(run);
    } else {
      groups.push({ key: run.run_id, runs: [run], name, displayStatus });
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
