import { describe, expect, it } from "vitest";

import { legacyRunId, runSessionId } from "./runIdentity";

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

  it("treats an empty id as absent rather than letting it erase a usable run_id", () => {
    expect(runSessionId({ id: "", run_id: "legacy-run" })).toBe("legacy-run");
  });

  it("returns empty only when neither identifier carries a value", () => {
    expect(runSessionId({ id: "", run_id: "" })).toBe("");
    expect(runSessionId({})).toBe("");
  });

  it("keeps sessions distinct when they share one compatibility scalar", () => {
    const ids = [
      runSessionId({ id: "session-1", run_id: "shared-run" }),
      runSessionId({ id: "session-2", run_id: "shared-run" }),
    ];

    expect(ids).toEqual(["session-1", "session-2"]);
  });
});

describe("legacyRunId", () => {
  it("reports the old key when the row has moved to a different session id", () => {
    expect(legacyRunId({ id: "session-1", run_id: "legacy-run" })).toBe("legacy-run");
  });

  it("reports nothing when the two identifiers agree", () => {
    expect(legacyRunId({ id: "same", run_id: "same" })).toBeNull();
  });

  it("reports nothing for a legacy-only row, whose current key already is run_id", () => {
    expect(legacyRunId({ run_id: "legacy-run" })).toBeNull();
  });

  it("reports nothing when there is no legacy value to fall back to", () => {
    expect(legacyRunId({ id: "session-1", run_id: "" })).toBeNull();
    expect(legacyRunId({ id: "session-1" })).toBeNull();
  });
});
