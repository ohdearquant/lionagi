import { describe, it, expect } from "vitest";
import { validateTeamsSearch } from "./index";

describe("validateTeamsSearch", () => {
  it("keeps a string s", () => {
    expect(validateTeamsSearch({ s: "team-1" })).toEqual({ s: "team-1" });
  });

  it("drops an empty or non-string s", () => {
    expect(validateTeamsSearch({ s: "" })).toEqual({});
    expect(validateTeamsSearch({ s: 42 })).toEqual({});
    expect(validateTeamsSearch({})).toEqual({});
  });
});
