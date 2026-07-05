/**
 * Data-source hook for Mission Control.
 *
 * Polls runs + invocations APIs every 3s. Drives a client-side watchdog:
 * if the fetch loop is silent for >5s, state transitions to "stale".
 * The reducer is the single integration point — swapping the poll for an
 * SSE subscription only requires changing this file.
 *
 * Hysteresis: stale badge appears after >5s silence. It clears only after
 * stable resumption (≥2 successful fetches), never on a single frame.
 */

import { useEffect, useReducer, useRef } from "react";
import { listRuns, listInvocations, listSchedules } from "@/lib/api";
import { boardReducer, initialBoardState } from "./boardReducer";
import type { BoardState } from "./boardReducer";

const POLL_INTERVAL_MS = 3_000;
const STALE_THRESHOLD_MS = 5_000;
const STABLE_RESUMPTION_COUNT = 2;

export function useLiveBoard(): BoardState {
  const [state, dispatch] = useReducer(boardReducer, undefined, initialBoardState);

  // Track consecutive successful fetches for hysteresis.
  const successStreak = useRef(0);
  const wasStaleRef = useRef(false);

  useEffect(() => {
    let active = true;

    // Watchdog: marks state stale if silent >5s
    let lastSuccessAt = Date.now();
    const watchdog = setInterval(() => {
      if (!active) return;
      if (Date.now() - lastSuccessAt > STALE_THRESHOLD_MS) {
        wasStaleRef.current = true;
        successStreak.current = 0;
        dispatch({ type: "MARK_STALE" });
      }
    }, 1_000);

    // Tick: update nowSec every second for ticking durations
    const ticker = setInterval(() => {
      if (!active) return;
      dispatch({ type: "TICK", nowSec: Math.floor(Date.now() / 1000) });
    }, 1_000);

    async function poll() {
      if (!active) return;
      try {
        const nowSec = Math.floor(Date.now() / 1000);
        // Schedules feed streak rows only — a failed fetch must not take
        // down the whole board, so it degrades to null (keep last-known).
        const [runsResp, invsResp, schedulesResp] = await Promise.all([
          listRuns({ per_page: 200 }),
          listInvocations({ limit: 100 }),
          listSchedules({ enabled: true }).catch(() => null),
        ]);
        if (!active) return;

        lastSuccessAt = Date.now();
        successStreak.current += 1;

        // Hysteresis: if we were stale, only clear after STABLE_RESUMPTION_COUNT
        if (!wasStaleRef.current || successStreak.current >= STABLE_RESUMPTION_COUNT) {
          wasStaleRef.current = false;
          dispatch({
            type: "DATA_OK",
            runs: runsResp.runs,
            invocations: invsResp.invocations,
            schedules: schedulesResp?.schedules ?? null,
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
