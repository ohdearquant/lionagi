/**
 * Data-source hook for the Pulse section.
 *
 * Deliberately slow: fetches on mount, every 45s, and on window focus —
 * never on the 3s live poll. Activity aggregates move on bucket
 * granularity (hours/days), so a fast cadence buys nothing.
 */

import { useEffect, useState } from "react";
import { getActivityStats } from "@/lib/api";
import type { ActivityStats, ActivityWindow } from "@/lib/api";

const REFRESH_INTERVAL_MS = 45_000;

export interface PulseState {
  data: ActivityStats | null;
  /** null = no failure; "" = failure without a message (localize at render). */
  error: string | null;
  loading: boolean;
}

export function usePulse(window_: ActivityWindow): PulseState {
  const [state, setState] = useState<PulseState>({
    data: null,
    error: null,
    loading: true,
  });

  useEffect(() => {
    // Effect-local guard: a response from a previous window selection can
    // never commit state after this effect is cleaned up.
    let active = true;
    setState({ data: null, error: null, loading: true });

    async function refresh() {
      try {
        const data = await getActivityStats(window_);
        if (!active) return;
        setState({ data, error: null, loading: false });
      } catch (err) {
        if (!active) return;
        setState((prev) => ({
          // Keep last-known data on a failed refresh; only the error surfaces.
          data: prev.data,
          error: err instanceof Error ? err.message : "",
          loading: false,
        }));
      }
    }

    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);

    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [window_]);

  return state;
}
