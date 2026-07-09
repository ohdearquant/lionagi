import { describe, it, expect } from "vitest";
import { runDeepLink, invocationDeepLink, scheduleDeepLink } from "./runDeepLink";

describe("runDeepLink", () => {
  it("routes to /fleet with the run id as the search param", () => {
    expect(runDeepLink("run-abc123")).toEqual({ to: "/fleet", search: { s: "run-abc123" } });
  });
});

describe("invocationDeepLink", () => {
  it("routes to /fleet with no search params", () => {
    expect(invocationDeepLink()).toEqual({ to: "/fleet" });
  });
});

describe("scheduleDeepLink", () => {
  it("routes to /schedules with the schedule id as the search param", () => {
    expect(scheduleDeepLink("sched-1")).toEqual({ to: "/schedules", search: { s: "sched-1" } });
  });
});
