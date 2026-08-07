// Pure state machine for "follow the run" (WorkerCanvas REQUIREMENT 4):
// during a live run the viewport may auto-pan to keep the running frontier
// in view, but ANY manual pan/zoom disables follow for the rest of that run,
// with a visible way to re-enable; a finished run never auto-moves. Kept
// framework-free (no React, no ReactFlow types) so the transition table is
// unit-testable without mounting a canvas; a `useReducer(followModeReducer,
// initialFollowModeState(...))` in WorkerCanvas is the intended wiring.

export interface FollowModeState {
  following: boolean;
  /** True while a `setCenter` call this state machine initiated is in
   * flight — used to tell an auto-pan's own `onMoveEnd` apart from the
   * user's hands on the wheel. */
  isProgrammaticPan: boolean;
  /** True once the viewer has deliberately turned follow off (manual
   * pan/zoom, or toggling it off) — tracked separately from `following`
   * so a false->live transition can tell "never turned on yet" apart from
   * "the viewer turned it off" and only auto-enable in the former case. */
  manuallyReleased: boolean;
}

export type FollowModeAction =
  | { type: "run_state_changed"; live: boolean; done: boolean }
  | { type: "manual_interaction" }
  | { type: "toggle" }
  | { type: "programmatic_pan_start" }
  | { type: "programmatic_pan_end" };

// Follow defaults on for a live, unfinished run and off otherwise — matching
// the contract's "true by default when live && !done".
export function initialFollowModeState(live: boolean, done: boolean): FollowModeState {
  return { following: live && !done, isProgrammaticPan: false, manuallyReleased: false };
}

export function followModeReducer(
  state: FollowModeState,
  action: FollowModeAction,
): FollowModeState {
  switch (action.type) {
    case "run_state_changed":
      // A run reaching its terminal state always turns follow off — a
      // finished run must never auto-move.
      if (action.done) return { ...state, following: false };
      // A run going live for the first time (mount always inits false/false,
      // since getSession's initial fetch never marks an active run live —
      // see RunDetail) is the normal path, not a resumed one: turn follow on
      // unless the viewer has already released it this run, mirroring
      // initialFollowModeState's own "true by default when live && !done".
      if (action.live && !state.following && !state.manuallyReleased) {
        return { ...state, following: true };
      }
      return state;

    case "manual_interaction":
      // A pan/zoom this state machine did not itself initiate is the
      // user's hands on the wheel — that always wins for the rest of the
      // run, until an explicit toggle or a fresh run re-initializes.
      if (state.isProgrammaticPan) return state;
      return { ...state, following: false, manuallyReleased: true };

    case "toggle": {
      const following = !state.following;
      // Toggling on is an activation, not a release: clear the flag so a
      // later live transition isn't stuck off. Toggling off is a deliberate
      // release, same as a manual pan.
      return { ...state, following, manuallyReleased: following ? false : true };
    }

    case "programmatic_pan_start":
      return { ...state, isProgrammaticPan: true };

    case "programmatic_pan_end":
      return { ...state, isProgrammaticPan: false };

    default:
      return state;
  }
}

// Whether the caller should issue an auto-center call right now. Centralizes
// the live/done gating so a caller cannot auto-pan a finished run just
// because `following` was left true from a stale state update.
export function shouldAutoCenter(state: FollowModeState, live: boolean, done: boolean): boolean {
  return state.following && live && !done;
}
