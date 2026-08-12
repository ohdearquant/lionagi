/**
 * Data-source hook for Mission Control.
 *
 * Polls one coherent, bounded active snapshot every 3s. Drives a client-side watchdog:
 * if the fetch loop is silent for >5s, state transitions to "stale".
 * The reducer is the single integration point — swapping the poll for an
 * SSE subscription only requires changing this file.
 *
 * Hysteresis: stale badge appears after >5s silence. It clears only after
 * stable resumption (≥2 successful fetches), never on a single frame.
 */

import { useEffect, useReducer, useRef } from "react";
import {
  getActiveSnapshot,
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
    const watchdog = setInterval(() => {
      if (!active) return;
      if (Date.now() - lastSuccessAt > STALE_THRESHOLD_MS) {
        // Only a board that has already shown data can go stale. Before the
        // first success there is no earlier state to flap back to, and arming
        // the hysteresis here would gate the very first render behind a run of
        // consecutive successes.
        if (!everSucceeded) return;
        // Deliberately does NOT reset successStreak. Silence means the current
        // fetch has not come back yet, which is what a slow backend looks
        // like, not a failure — the catch path owns that. Clearing the streak
        // on every tick of a gap longer than the threshold makes the streak
        // unreachable whenever a fetch is slower than STALE_THRESHOLD_MS, and
        // the board then stays on its loading skeleton through any number of
        // successful fetches.
        wasStaleRef.current = true;
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
        const [snapshot, schedulesResp, dispositionsResp, gatedPlaysResp] = await Promise.all([
          getActiveSnapshot({ run_limit: 200, invocation_limit: 100, recent_limit: 200 }),
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
          dispatch({
            type: "DATA_OK",
            runs: [...snapshot.active_runs, ...snapshot.recent_runs],
            invocations: [...snapshot.active_invocations, ...snapshot.recent_invocations],
            activeRunTotal: snapshot.active_run_total,
            activeInvocationTotal: snapshot.active_invocation_total,
            activeRunOmitted: snapshot.active_run_omitted,
            activeInvocationOmitted: snapshot.active_invocation_omitted,
            snapshotVersion: snapshot.snapshot_version,
            schedules: schedulesResp?.schedules ?? null,
            dispositions: dispositionsResp,
            gatedPlays: gatedPlaysResp,
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
