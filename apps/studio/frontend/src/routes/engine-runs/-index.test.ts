import { describe, it, expect } from "vitest";
import { validateEngineRunsSearch } from "./index";

describe("validateEngineRunsSearch", () => {
  it("keeps kind, status, session_id, and s together", () => {
    expect(
      validateEngineRunsSearch({
        kind: "coding",
        status: "failed",
        session_id: "sess-1",
        s: "run-1",
      }),
    ).toEqual({ kind: "coding", status: "failed", session_id: "sess-1", s: "run-1" });
  });

  it("drops empty or non-string values", () => {
    expect(validateEngineRunsSearch({ kind: "", status: 3, session_id: undefined })).toEqual({});
  });

  it("keeps every backend-recognized status", () => {
    for (const status of ["running", "completed", "failed", "cancelled"]) {
      expect(validateEngineRunsSearch({ status })).toEqual({ status });
    }
  });

  it("rejects a status the backend never emits, rather than passing it through as an unfiltered request", () => {
    expect(validateEngineRunsSearch({ status: "never-emitted" })).toEqual({});
  });

  it("returns an empty object for no search", () => {
    expect(validateEngineRunsSearch({})).toEqual({});
  });

  it("supports a session_id-only deep link (the SessionDetail entry point)", () => {
    expect(validateEngineRunsSearch({ session_id: "sess-42" })).toEqual({
      session_id: "sess-42",
    });
  });
});
