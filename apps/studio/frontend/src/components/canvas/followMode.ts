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
  return { following: live && !done, isProgrammaticPan: false };
}

export function followModeReducer(
  state: FollowModeState,
  action: FollowModeAction,
): FollowModeState {
  switch (action.type) {
    case "run_state_changed":
      // A run reaching its terminal state always turns follow off — a
      // finished run must never auto-move. Going live does not force
      // following back on by itself: a fresh run re-initializes via
      // initialFollowModeState instead, so a viewer's earlier manual
      // interruption is never silently overridden mid-run.
      return action.done ? { ...state, following: false } : state;

    case "manual_interaction":
      // A pan/zoom this state machine did not itself initiate is the
      // user's hands on the wheel — that always wins for the rest of the
      // run, until an explicit toggle or a fresh run re-initializes.
      if (state.isProgrammaticPan) return state;
      return { ...state, following: false };

    case "toggle":
      return { ...state, following: !state.following };

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
