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
import {
  listRuns,
  listInvocations,
  listSchedules,
  listAttentionDispositions,
  listGatedPlays,
} from "@/lib/api";
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
    let inFlight = false;

    // Watchdog: marks state stale if silent >5s
    let lastSuccessAt = Date.now();
    let everSucceeded = false;
    // The watchdog reads the state it writes, so it may dispatch only on the
    // transition into stale. Re-dispatching every tick while the badge is
    // already stale keeps a settled board committing forever; the reducer
    // absorbs it, but React still re-renders on each dispatch.
    let staleDispatched = false;
    const watchdog = setInterval(() => {
      if (!active) return;
      if (staleDispatched) return;
      if (Date.now() - lastSuccessAt > STALE_THRESHOLD_MS) {
        // Only a board that has already shown data can go stale. Before the
        // first success there is no earlier state to flap back to, and arming
        // the hysteresis here would gate the very first render behind a run of
        // consecutive successes.
        if (!everSucceeded) return;
        // A board whose last fetch failed is already showing the error, which
        // a stale mark does not replace. Arming the hysteresis there would hold
        // back the first success after the failure, and the error would stay
        // on screen with fresh data in hand.
        if (successStreak.current === 0) return;
        // Resumption counts from the first success after the silence: a
        // success that landed before it says nothing about whether the
        // backend is answering again. The streak is cleared once, here, on the
        // transition, and never on later ticks: staleDispatched holds the
        // watchdog off until the board resumes. Clearing it on every tick of a
        // gap longer than the threshold would make the streak unreachable
        // whenever a fetch is slower than STALE_THRESHOLD_MS, and the board
        // would then stay stale through any number of successful fetches.
        successStreak.current = 0;
        wasStaleRef.current = true;
        staleDispatched = true;
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
      // A tick that arrives while the previous poll is still outstanding is
      // dropped, not queued. Without this the interval keeps opening requests
      // regardless of how long they take, so once a fetch is slower than
      // POLL_INTERVAL_MS the outstanding count grows without bound and the
      // board never receives a first response at all.
      if (inFlight) return;
      inFlight = true;
      try {
        const nowSec = Math.floor(Date.now() / 1000);
        // Schedules, dispositions, and gated plays each feed one part of the
        // board only — a failed fetch must not take down the whole board, so
        // all three degrade to null (keep last-known) rather than rejecting
        // the poll.
        const [runsResp, invsResp, schedulesResp, dispositionsResp, gatedPlaysResp] =
          await Promise.all([
            listRuns({ per_page: 200 }),
            listInvocations({ limit: 100 }),
            listSchedules({ enabled: true }).catch(() => null),
            listAttentionDispositions().catch(() => null),
            listGatedPlays().catch(() => null),
          ]);
        if (!active) return;

        lastSuccessAt = Date.now();
        everSucceeded = true;
        successStreak.current += 1;

        // Hysteresis: if we were stale, only clear after STABLE_RESUMPTION_COUNT
        if (!wasStaleRef.current || successStreak.current >= STABLE_RESUMPTION_COUNT) {
          wasStaleRef.current = false;
          // Re-armed only once the board has resumed. A success the
          // hysteresis holds back has not resumed it, so a silence after that
          // success is no new transition to report.
          staleDispatched = false;
          dispatch({
            type: "DATA_OK",
            runs: runsResp.runs,
            invocations: invsResp.invocations,
            schedules: schedulesResp?.schedules ?? null,
            dispositions: dispositionsResp,
            gatedPlays: gatedPlaysResp,
            nowSec,
          });
        }
      } catch (err) {
        if (!active) return;
        successStreak.current = 0;
        // The error replaces a stale mark on screen, so the next success is a
        // recovery from the error, which the hysteresis does not hold back (see
        // the watchdog). Leaving the mark armed would keep the error showing
        // with fresh data in hand.
        wasStaleRef.current = false;
        dispatch({
          type: "DATA_ERROR",
          message: err instanceof Error ? err.message : "API unreachable",
        });
      } finally {
        // Both branches above return early once unmounted, so releasing the
        // guard anywhere but here would leave it held and stop polling for
        // good on the next remount.
        inFlight = false;
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
