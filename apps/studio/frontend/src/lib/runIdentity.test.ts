import { describe, expect, it } from "vitest";

import { runSessionId } from "./runIdentity";

describe("runSessionId", () => {
  it("uses id when the compatibility run_id scalar differs", () => {
    expect(runSessionId({ id: "session-1", run_id: "shared-run" })).toBe("session-1");
  });

  it("keeps legacy rows addressable through run_id", () => {
    expect(runSessionId({ run_id: "legacy-run" })).toBe("legacy-run");
  });

  it("does not let a nullable compatibility scalar erase session identity", () => {
    expect(runSessionId({ id: "session-1", run_id: null })).toBe("session-1");
  });

  it("keeps sessions distinct when they share one compatibility scalar", () => {
    const ids = [
      runSessionId({ id: "session-1", run_id: "shared-run" }),
      runSessionId({ id: "session-2", run_id: "shared-run" }),
    ];

    expect(ids).toEqual(["session-1", "session-2"]);
  });
});
