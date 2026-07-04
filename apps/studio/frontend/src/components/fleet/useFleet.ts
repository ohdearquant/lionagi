/**
 * Data-source hook for Fleet view.
 *
 * Polls invocations + runs every 3s via Promise.all. Client-side watchdog
 * transitions to "stale" after >5s silence. Stale clears only after
 * ≥2 consecutive successful fetches (hysteresis). SSE can replace polling
 * later by changing only this file — reducer and components are unchanged.
 */

import { useEffect, useReducer, useRef } from "react";
import { listInvocations, listRuns } from "@/lib/api";
import { fleetReducer, initialFleetState } from "./fleetReducer";
import type { FleetState } from "./fleetReducer";

const POLL_INTERVAL_MS = 3_000;
const STALE_THRESHOLD_MS = 5_000;
const STABLE_RESUMPTION_COUNT = 2;

export function useFleet(): FleetState {
  const [state, dispatch] = useReducer(fleetReducer, undefined, initialFleetState);

  const successStreak = useRef(0);
  const wasStaleRef = useRef(false);

  useEffect(() => {
    let active = true;
    let lastSuccessAt = Date.now();

    const watchdog = setInterval(() => {
      if (!active) return;
      if (Date.now() - lastSuccessAt > STALE_THRESHOLD_MS) {
        wasStaleRef.current = true;
        successStreak.current = 0;
        dispatch({ type: "MARK_STALE" });
      }
    }, 1_000);

    const ticker = setInterval(() => {
      if (!active) return;
      dispatch({ type: "TICK", nowSec: Math.floor(Date.now() / 1000) });
    }, 30_000);

    async function poll() {
      if (!active) return;
      try {
        const nowSec = Math.floor(Date.now() / 1000);
        const [invsResp, runsResp] = await Promise.all([
          listInvocations({ limit: 200 }),
          listRuns({ per_page: 200 }),
        ]);
        if (!active) return;

        lastSuccessAt = Date.now();
        successStreak.current += 1;

        if (!wasStaleRef.current || successStreak.current >= STABLE_RESUMPTION_COUNT) {
          wasStaleRef.current = false;
          dispatch({
            type: "DATA_OK",
            invocations: invsResp.invocations,
            runs: runsResp.runs,
            nowSec,
          });
        }
      } catch (err) {
        if (!active) return;
        successStreak.current = 0;
        dispatch({
          type: "DATA_ERROR",
          message: err instanceof Error ? err.message : "API unreachable",
        });
      }
    }

    void poll();
    const poller = setInterval(() => void poll(), POLL_INTERVAL_MS);

    return () => {
      active = false;
      clearInterval(watchdog);
      clearInterval(ticker);
      clearInterval(poller);
    };
  }, []);

  return state;
}
