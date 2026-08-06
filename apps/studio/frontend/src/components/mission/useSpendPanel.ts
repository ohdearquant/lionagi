/**
 * Data-source hook for the spend panel — mirrors usePulse.ts's cadence and
 * stale-window guard exactly (same 45s refresh, same effect-local `active`
 * flag) so a late response from a previous window selection can never
 * commit into the current one.
 */

import { useEffect, useState } from "react";
import { getSpendStats } from "@/lib/api";
import type { SpendStats, ActivityWindow } from "@/lib/api";

const REFRESH_INTERVAL_MS = 45_000;
// Window focus can fire repeatedly in a burst (alt-tab spam, multi-monitor
// window managers) — a focus-triggered refresh only fires if the last one
// (of any origin: mount, interval, or a prior focus) is at least this old.
const MIN_FOCUS_REFRESH_AGE_MS = 5_000;

export interface SpendPanelState {
  data: SpendStats | null;
  /** null = no failure; "" = failure without a message (localize at render). */
  error: string | null;
  loading: boolean;
}

export function useSpendPanel(window_: ActivityWindow): SpendPanelState {
  const [state, setState] = useState<SpendPanelState>({
    data: null,
    error: null,
    loading: true,
  });

  useEffect(() => {
    let active = true;
    let inFlight = false;
    let lastRefreshAt = 0;
    setState({ data: null, error: null, loading: true });

    async function refresh() {
      if (inFlight) return;
      inFlight = true;
      lastRefreshAt = Date.now();
      try {
        const data = await getSpendStats(window_);
        if (!active) return;
        setState({ data, error: null, loading: false });
      } catch (err) {
        if (!active) return;
        setState((prev) => ({
          data: prev.data,
          error: err instanceof Error ? err.message : "",
          loading: false,
        }));
      } finally {
        inFlight = false;
      }
    }

    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    const onFocus = () => {
      if (inFlight || Date.now() - lastRefreshAt < MIN_FOCUS_REFRESH_AGE_MS) return;
      void refresh();
    };
    window.addEventListener("focus", onFocus);

    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [window_]);

  return state;
}
