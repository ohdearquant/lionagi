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
// Four poll intervals, because the threshold has to clear one slow RESPONSE
// rather than one interval. A poll slower than POLL_INTERVAL_MS gets the next
// tick dropped as in-flight, so the gap between two successes jumps to two
// intervals with nothing actually wrong. At 5s that meant any response slower
// than about three seconds flipped the view to "stale" -- which the paired
// 200-row fetches below reach routinely -- and the badge then flapped
// continuously against a healthy backend. A backend that is genuinely gone
// still surfaces within twelve seconds.
export const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;
const STABLE_RESUMPTION_COUNT = 2;

export interface FleetFilters {
  project?: string;
  projectNull?: boolean;
  search?: string;
  /** Orchestration-kind facet: agent | play | flow | fanout | show. */
  kind?: string;
}

export function useFleet(filters?: FleetFilters): FleetState {
  const [state, dispatch] = useReducer(fleetReducer, undefined, initialFleetState);

  const successStreak = useRef(0);
  const wasStaleRef = useRef(false);
  const project = filters?.project;
  const projectNull = filters?.projectNull ?? false;
  const search = filters?.search;
  const kind = filters?.kind;

  useEffect(() => {
    let active = true;
    let inFlight = false;
    let lastSuccessAt = Date.now();
    let everSucceeded = false;

    const watchdog = setInterval(() => {
      if (!active) return;
      if (Date.now() - lastSuccessAt > STALE_THRESHOLD_MS) {
        // Only a view that has already shown data can go stale. Before the
        // first success there is no earlier state to flap back to, and arming
        // the hysteresis here would gate the very first render behind a run of
        // consecutive successes.
        if (!everSucceeded) return;
        // Deliberately does NOT reset successStreak. Silence means the current
        // fetch has not come back yet, which is what a slow backend looks
        // like, not a failure — the catch path owns that. Clearing the streak
        // on every tick of a gap longer than the threshold makes the streak
        // unreachable whenever a fetch is slower than STALE_THRESHOLD_MS, and
        // the view then stays on its loading skeleton through any number of
        // successful fetches.
        wasStaleRef.current = true;
        dispatch({ type: "MARK_STALE" });
      }
    }, 1_000);

    const ticker = setInterval(() => {
      if (!active) return;
      dispatch({ type: "TICK", nowSec: Math.floor(Date.now() / 1000) });
    }, 30_000);

    async function poll() {
      if (!active) return;
      // A tick that arrives while the previous poll is still outstanding is
      // dropped, not queued. Without this the interval keeps opening requests
      // regardless of how long they take, so once a fetch is slower than
      // POLL_INTERVAL_MS the outstanding count grows without bound and the
      // view never receives a first response at all.
      if (inFlight) return;
      inFlight = true;
      try {
        const nowSec = Math.floor(Date.now() / 1000);
        const [invsResp, runsResp] = await Promise.all([
          listInvocations({ limit: 200 }),
          listRuns({
            per_page: 200,
            project,
            project_null: projectNull,
            search,
            kind: kind ? [kind] : undefined,
          }),
        ]);
        if (!active) return;

        lastSuccessAt = Date.now();
        everSucceeded = true;
        successStreak.current += 1;

        if (!wasStaleRef.current || successStreak.current >= STABLE_RESUMPTION_COUNT) {
          wasStaleRef.current = false;
          dispatch({
            type: "DATA_OK",
            invocations: invsResp.invocations,
            runs: runsResp.runs,
            runsHasNext: runsResp.has_next,
            nowSec,
            project,
            projectNull,
            search,
            kind,
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
    // Changing a filter restarts polling immediately (via the effect's own
    // teardown/setup) rather than waiting up to POLL_INTERVAL_MS for the next
    // tick to pick up the new scope.
  }, [project, projectNull, search, kind]);

  return state;
}
