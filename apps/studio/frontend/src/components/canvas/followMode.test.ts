import { describe, it, expect } from "vitest";
import {
  followModeReducer,
  initialFollowModeState,
  shouldAutoCenter,
  type FollowModeState,
} from "./followMode";

describe("initialFollowModeState", () => {
  it("follows by default on a live, unfinished run", () => {
    expect(initialFollowModeState(true, false)).toEqual({
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    });
  });

  it("does not follow when the run is not live", () => {
    expect(initialFollowModeState(false, false).following).toBe(false);
  });

  it("does not follow a run that is already done", () => {
    expect(initialFollowModeState(true, true).following).toBe(false);
  });
});

describe("followModeReducer — manual interruption", () => {
  it("a genuine manual pan/zoom disables follow", () => {
    const state: FollowModeState = {
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    const next = followModeReducer(state, { type: "manual_interaction" });
    expect(next.following).toBe(false);
  });

  it("an auto-pan's own onMoveEnd (isProgrammaticPan=true) does not count as manual", () => {
    const state: FollowModeState = {
      following: true,
      isProgrammaticPan: true,
      manuallyReleased: false,
    };
    const next = followModeReducer(state, { type: "manual_interaction" });
    expect(next.following).toBe(true);
  });

  it("manual interruption persists across further non-toggle actions", () => {
    let state: FollowModeState = {
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    state = followModeReducer(state, { type: "manual_interaction" });
    state = followModeReducer(state, { type: "run_state_changed", live: true, done: false });
    expect(state.following).toBe(false);
  });
});

describe("followModeReducer — toggle re-enables", () => {
  it("toggle flips off->on", () => {
    const state: FollowModeState = {
      following: false,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    expect(followModeReducer(state, { type: "toggle" }).following).toBe(true);
  });

  it("toggle flips on->off", () => {
    const state: FollowModeState = {
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    expect(followModeReducer(state, { type: "toggle" }).following).toBe(false);
  });
});

describe("followModeReducer — a completed run never auto-moves", () => {
  it("run_state_changed with done=true forces following off even if it was on", () => {
    const state: FollowModeState = {
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    const next = followModeReducer(state, { type: "run_state_changed", live: false, done: true });
    expect(next.following).toBe(false);
  });

  it("toggling on a done run does not make shouldAutoCenter true", () => {
    let state: FollowModeState = {
      following: false,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    state = followModeReducer(state, { type: "toggle" });
    expect(state.following).toBe(true); // the toggle itself is honest about state...
    expect(shouldAutoCenter(state, false, true)).toBe(false); // ...but done gates the actual auto-pan
  });
});

describe("followModeReducer — the normal first live transition (RunDetail mounts false/false, then goes live)", () => {
  it("run_state_changed false->live true enables follow when the user never released it", () => {
    const state: FollowModeState = {
      following: false,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    const next = followModeReducer(state, { type: "run_state_changed", live: true, done: false });
    expect(next.following).toBe(true);
  });

  it("run_state_changed false->live true stays off if the user already manually released follow", () => {
    let state: FollowModeState = {
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    state = followModeReducer(state, { type: "manual_interaction" });
    expect(state.following).toBe(false);
    const next = followModeReducer(state, { type: "run_state_changed", live: true, done: false });
    expect(next.following).toBe(false);
  });

  it("full sequence: mount false/false -> live true enables follow -> manual pan releases -> re-mounting live true does not resurrect it", () => {
    let state = initialFollowModeState(false, false);
    expect(state.following).toBe(false);

    state = followModeReducer(state, { type: "run_state_changed", live: true, done: false });
    expect(state.following).toBe(true);

    state = followModeReducer(state, { type: "manual_interaction" });
    expect(state.following).toBe(false);

    state = followModeReducer(state, { type: "run_state_changed", live: true, done: false });
    expect(state.following).toBe(false);
  });
});

describe("followModeReducer — programmatic pan bracket", () => {
  it("start sets isProgrammaticPan, end clears it", () => {
    let state: FollowModeState = {
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    state = followModeReducer(state, { type: "programmatic_pan_start" });
    expect(state.isProgrammaticPan).toBe(true);
    state = followModeReducer(state, { type: "programmatic_pan_end" });
    expect(state.isProgrammaticPan).toBe(false);
  });
});

describe("shouldAutoCenter", () => {
  it("true only when following, live, and not done", () => {
    const state: FollowModeState = {
      following: true,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    expect(shouldAutoCenter(state, true, false)).toBe(true);
    expect(shouldAutoCenter(state, false, false)).toBe(false);
    expect(shouldAutoCenter(state, true, true)).toBe(false);
  });

  it("false when not following even if live and not done", () => {
    const state: FollowModeState = {
      following: false,
      isProgrammaticPan: false,
      manuallyReleased: false,
    };
    expect(shouldAutoCenter(state, true, false)).toBe(false);
  });
});
