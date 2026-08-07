import { useEffect, useState } from "react";
import { listInvocations } from "@/lib/api";
import type { InvocationSummary } from "@/lib/api";

export interface InvocationStats {
  total: number;
  successRate: number | null;
  lastUsedSec: number | null;
  recent: InvocationSummary[];
}

const RECENT_LIMIT = 5;

/**
 * Usage stats for a skill or plugin's Library detail panel. `total` and
 * `successRate` come from the server's real counts (ADR: invocation counts
 * stop silently capping at the page limit) rather than the size of whatever
 * page happened to be fetched, and both skill and plugin filter server-side
 * — a plugin's stats are no longer a best-effort sample over the last 200
 * invocations system-wide.
 */
export function useInvocationStats(
  kind: "skill" | "plugin",
  name: string,
): {
  stats: InvocationStats | null;
  loading: boolean;
} {
  const [stats, setStats] = useState<InvocationStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch; setState fires synchronously in effect body only, callbacks are guarded by alive flag
    setStats(null);
    setLoading(true);

    const scope = kind === "skill" ? { skill: name } : { plugin: name };

    listInvocations({ ...scope, limit: RECENT_LIMIT })
      .then((res) => {
        if (!alive) return;
        const successRate =
          res.total > 0 ? Math.round((res.completed_total / res.total) * 100) : null;
        // Rows are ordered by updated_at DESC, so the first row is already
        // the most recently touched invocation — no need to scan for a max.
        const first = res.invocations[0];
        const lastUsedSec = first ? (first.ended_at ?? first.started_at) : null;
        setStats({ total: res.total, successRate, lastUsedSec, recent: res.invocations });
        setLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        setStats(null);
        setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [kind, name]);

  return { stats, loading };
}

export function formatInvocationAge(epochSec: number): string {
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
  if (diffSec < 60) return `${diffSec}s`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  return `${Math.floor(diffSec / 86400)}d`;
}
