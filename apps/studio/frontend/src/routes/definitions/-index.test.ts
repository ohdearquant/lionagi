import { describe, it, expect } from "vitest";
import { validateDefinitionsSearch, libraryHref } from "./index";
import type { DefinitionSummary } from "@/lib/api";

function def(overrides: Partial<DefinitionSummary> = {}): DefinitionSummary {
  return {
    kind: "agent",
    name: "reviewer",
    path: "agents/reviewer.md",
    disk_path: "agents/reviewer.md",
    has_versions: true,
    version: 3,
    updated_at: 0,
    ...overrides,
  };
}

describe("validateDefinitionsSearch", () => {
  it("keeps a non-empty kind", () => {
    expect(validateDefinitionsSearch({ kind: "agent" })).toEqual({ kind: "agent" });
  });

  it("drops an empty or non-string kind", () => {
    expect(validateDefinitionsSearch({ kind: "" })).toEqual({});
    expect(validateDefinitionsSearch({ kind: 1 })).toEqual({});
    expect(validateDefinitionsSearch({})).toEqual({});
  });

  it("keeps every backend-recognized kind", () => {
    expect(validateDefinitionsSearch({ kind: "agent" })).toEqual({ kind: "agent" });
    expect(validateDefinitionsSearch({ kind: "playbook" })).toEqual({ kind: "playbook" });
  });

  it("rejects a kind the backend never emits, rather than passing it through as an unfiltered request", () => {
    expect(validateDefinitionsSearch({ kind: "widget" })).toEqual({});
  });
});

describe("libraryHref — routes a definition to its existing Library editor", () => {
  it("maps an agent definition to the agent tab/sel", () => {
    expect(libraryHref(def({ kind: "agent", name: "reviewer" }))).toEqual({
      tab: "agent",
      sel: "agent:reviewer",
    });
  });

  it("maps a playbook definition to the playbook tab with the custom sub-kind", () => {
    // Definitions only scans ~/.lionagi/playbooks (user-authored copies), not
    // the bundled builtin templates, so every playbook here is "custom".
    expect(libraryHref(def({ kind: "playbook", name: "nightly-review" }))).toEqual({
      tab: "playbook",
      sel: "playbook:custom:nightly-review",
    });
  });
});
