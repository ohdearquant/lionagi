/**
 * Fleet search contract tests — Fleet is the redirect target for every
 * retired route, so its validateSearch must keep the filters those old URLs
 * carried (status/playbook/project/page/skill/sessions/invocation), not just
 * the `s` selection param.
 */
import { describe, it, expect } from "vitest";
import { validateFleetSearch } from "./fleet";

describe("validateFleetSearch", () => {
  it("returns { s } for a valid run id string", () => {
    expect(validateFleetSearch({ s: "run-1" })).toEqual({ s: "run-1" });
  });

  it("preserves status/playbook/project filters alongside s", () => {
    expect(
      validateFleetSearch({
        s: "run-1",
        status: ["running", "pending"],
        playbook: "daily",
        project: "ocean",
      }),
    ).toEqual({
      s: "run-1",
      status: ["running", "pending"],
      playbook: "daily",
      project: "ocean",
    });
  });

  it("preserves sessions arrays and unknown primitive keys", () => {
    expect(validateFleetSearch({ sessions: ["s1", "s2"], extra: "x" })).toEqual({
      sessions: ["s1", "s2"],
      extra: "x",
    });
  });

  it("preserves invocation, skill, and page", () => {
    expect(validateFleetSearch({ invocation: "inv-1", skill: "review", page: 2 })).toEqual({
      invocation: "inv-1",
      skill: "review",
      page: 2,
    });
  });

  it("removes s when empty", () => {
    expect(validateFleetSearch({ s: "", status: "running" })).toEqual({ status: "running" });
  });

  it("chooses the first non-empty string when s is an array", () => {
    expect(validateFleetSearch({ s: ["", "run-1", "run-2"] })).toEqual({ s: "run-1" });
  });

  it("drops object-valued params", () => {
    expect(validateFleetSearch({ s: "run-1", weird: { nested: true } })).toEqual({ s: "run-1" });
  });

  it("returns an empty object for no search", () => {
    expect(validateFleetSearch({})).toEqual({});
  });

  // ─── project / project_null / q — Fleet filter state (URL-persisted) ──────

  it("parses a project filter to a clean string", () => {
    expect(validateFleetSearch({ project: "org/alpha" })).toEqual({ project: "org/alpha" });
  });

  it("takes the first non-empty string when project arrives as an array", () => {
    expect(validateFleetSearch({ project: ["", "org/alpha", "org/beta"] })).toEqual({
      project: "org/alpha",
    });
  });

  it("parses project_null from a real boolean or the string 'true'", () => {
    expect(validateFleetSearch({ project_null: true })).toEqual({ project_null: true });
    expect(validateFleetSearch({ project_null: "true" })).toEqual({ project_null: true });
  });

  it("drops project_null when false or absent", () => {
    expect(validateFleetSearch({ project_null: false })).toEqual({});
    expect(validateFleetSearch({})).not.toHaveProperty("project_null");
  });

  it("parses the search text as q", () => {
    expect(validateFleetSearch({ q: "flaky login" })).toEqual({ q: "flaky login" });
  });

  it("drops an empty q rather than carrying an empty-string filter", () => {
    expect(validateFleetSearch({ q: "" })).toEqual({});
  });

  it("combines s, project, project_null, and q together", () => {
    expect(
      validateFleetSearch({ s: "run-1", project: "org/alpha", project_null: true, q: "flaky" }),
    ).toEqual({ s: "run-1", project: "org/alpha", project_null: true, q: "flaky" });
  });
});
