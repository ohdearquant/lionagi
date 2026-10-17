import { useCallback, useEffect, useState } from "react";
import { listInvocations } from "@/lib/api";
import type { InvocationSummary } from "@/lib/api";

export interface InvocationStats {
  total: number;
  successRate: number | null;
  lastUsedSec: number | null;
  recent: InvocationSummary[];
}

const RECENT_LIMIT = 5;

/** Recorded usage totals and start time, independent of the recent-page limit. */
export function useInvocationStats(
  kind: "skill" | "plugin",
  name: string,
): {
  stats: InvocationStats | null;
  loading: boolean;
  error: boolean;
  retry: () => void;
} {
  const [stats, setStats] = useState<InvocationStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((current) => current + 1), []);

  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch; setState fires synchronously in effect body only, callbacks are guarded by alive flag
    setStats(null);
    setLoading(true);
    setError(false);

    const scope = kind === "skill" ? { skill: name } : { plugin: name };

    listInvocations({ ...scope, limit: RECENT_LIMIT, includeUsage: true })
      .then((res) => {
        if (!alive) return;
        const successRate =
          res.finished_total != null && res.finished_total > 0
            ? Math.round((res.completed_total / res.finished_total) * 100)
            : null;
        // Older servers omit the aggregate; a bounded page cannot replace it.
        const lastUsedSec = res.latest_started_at ?? null;
        setStats({ total: res.total, successRate, lastUsedSec, recent: res.invocations });
        setLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        setStats(null);
        setError(true);
        setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [kind, name, attempt]);

  return { stats, loading, error, retry };
}

export function formatInvocationAge(epochSec: number): string {
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
  if (diffSec < 60) return `${diffSec}s`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  return `${Math.floor(diffSec / 86400)}d`;
}
