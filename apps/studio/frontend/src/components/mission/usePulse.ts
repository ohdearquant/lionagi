/**
 * Data-source hook for the Pulse section.
 *
 * Deliberately slow: fetches on mount, every 45s, and on window focus —
 * never on the 3s live poll. Activity aggregates move on bucket
 * granularity (hours/days), so a fast cadence buys nothing.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getActivityStats } from "@/lib/api";
import type { ActivityStats, ActivityWindow } from "@/lib/api";

const REFRESH_INTERVAL_MS = 45_000;

export interface PulseState {
  data: ActivityStats | null;
  error: string | null;
  loading: boolean;
}

export function usePulse(window_: ActivityWindow): PulseState {
  const [state, setState] = useState<PulseState>({
    data: null,
    error: null,
    loading: true,
  });
  const activeRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const data = await getActivityStats(window_);
      if (!activeRef.current) return;
      setState({ data, error: null, loading: false });
    } catch (err) {
      if (!activeRef.current) return;
      setState((prev) => ({
        // Keep last-known data on a failed refresh; only the error surfaces.
        data: prev.data,
        error: err instanceof Error ? err.message : "API unreachable",
        loading: false,
      }));
    }
  }, [window_]);

  useEffect(() => {
    activeRef.current = true;
    setState((prev) => ({ ...prev, loading: prev.data === null }));
    void refresh();

    const timer = setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);

    return () => {
      activeRef.current = false;
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  return state;
}
