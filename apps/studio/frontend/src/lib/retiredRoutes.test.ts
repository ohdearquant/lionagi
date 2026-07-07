import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  firstSearchString,
  preserveRetiredSearch,
  mergeRetiredSearch,
  retiredRedirect,
  retiredInvocationRedirect,
  formatRetiredRouteError,
} from "./retiredRoutes";
import type { InvocationDetail } from "@/lib/api";

function makeInvocation(sessionIds: string[]): InvocationDetail {
  return {
    id: "inv-1",
    skill: "review",
    status: "completed",
    plugin: null,
    prompt: null,
    started_at: 1_000_000,
    ended_at: null,
    session_count: sessionIds.length,
    created_at: 0,
    updated_at: 0,
    node_metadata: null,
    project: null,
    project_source: null,
    sessions: sessionIds.map((id) => ({
      id,
      name: null,
      agent_name: null,
      playbook_name: null,
      invocation_kind: null,
      status: null,
      last_message_at: null,
      started_at: null,
      ended_at: null,
    })),
    artifacts: [],
  };
}

// ─── preserveRetiredSearch ─────────────────────────────────────────────────

describe("preserveRetiredSearch", () => {
  it("preserves non-empty strings, numbers, booleans", () => {
    expect(preserveRetiredSearch({ status: "running", page: 2, active: true })).toEqual({
      status: "running",
      page: 2,
      active: true,
    });
  });

  it("preserves arrays of primitives", () => {
    expect(preserveRetiredSearch({ status: ["running", "pending"] })).toEqual({
      status: ["running", "pending"],
    });
  });

  it("preserves unknown primitive keys", () => {
    expect(preserveRetiredSearch({ project: "ocean", s: "run-1" })).toEqual({
      project: "ocean",
      s: "run-1",
    });
  });

  it("drops null, undefined, empty strings, empty arrays, objects, and functions", () => {
    expect(
      preserveRetiredSearch({
        a: null,
        b: undefined,
        c: "",
        d: [],
        e: { x: 1 },
        f: () => {},
      }),
    ).toEqual({});
  });

  it("drops empty-string items out of arrays but keeps the rest", () => {
    expect(preserveRetiredSearch({ status: ["", "running", ""] })).toEqual({
      status: ["running"],
    });
  });
});

// ─── firstSearchString ──────────────────────────────────────────────────────

describe("firstSearchString", () => {
  it("returns a non-empty scalar string", () => {
    expect(firstSearchString("run-1")).toBe("run-1");
  });

  it("returns the first non-empty string from an array", () => {
    expect(firstSearchString(["", "run-1", "run-2"])).toBe("run-1");
  });

  it("returns undefined for empty string, non-string, or all-empty array", () => {
    expect(firstSearchString("")).toBeUndefined();
    expect(firstSearchString(42)).toBeUndefined();
    expect(firstSearchString(["", ""])).toBeUndefined();
  });
});

// ─── retiredRedirect / mergeRetiredSearch ──────────────────────────────────

describe("retiredRedirect", () => {
  it("preserves incoming filters and applies overrides", () => {
    const target = retiredRedirect(
      "/fleet",
      { status: "running", playbook: "daily" },
      { s: "run-1" },
    );
    expect(target).toEqual({
      to: "/fleet",
      search: { status: "running", playbook: "daily", s: "run-1" },
    });
  });

  it("overrides win over incoming search on key collision", () => {
    const target = retiredRedirect("/fleet", { s: "incoming" }, { s: "override" });
    expect(target.search.s).toBe("override");
  });

  it("returns just the override when no incoming search is given", () => {
    expect(retiredRedirect("/library", undefined, { tab: "skill" })).toEqual({
      to: "/library",
      search: { tab: "skill" },
    });
  });
});

describe("mergeRetiredSearch", () => {
  it("sanitizes both incoming search and overrides", () => {
    expect(mergeRetiredSearch({ status: "" }, { tab: "skill", junk: {} })).toEqual({
      tab: "skill",
    });
  });
});

// ─── retiredInvocationRedirect ──────────────────────────────────────────────

describe("retiredInvocationRedirect", () => {
  it("selects the incoming ?s= session when it matches a returned session", async () => {
    const target = await retiredInvocationRedirect(
      "inv-1",
      { s: "sess-2" },
      { getInvocation: async () => makeInvocation(["sess-1", "sess-2"]) },
    );
    expect(target).toEqual({
      to: "/fleet",
      search: { s: "sess-2", sessions: ["sess-1", "sess-2"] },
    });
  });

  it("falls back to the first returned session when ?s= doesn't match", async () => {
    const target = await retiredInvocationRedirect(
      "inv-1",
      { s: "not-a-session" },
      { getInvocation: async () => makeInvocation(["sess-1", "sess-2"]) },
    );
    expect(target.search.s).toBe("sess-1");
  });

  it("falls back to the first returned session when no ?s= is given", async () => {
    const target = await retiredInvocationRedirect(
      "inv-1",
      {},
      { getInvocation: async () => makeInvocation(["sess-1"]) },
    );
    expect(target.search.s).toBe("sess-1");
  });

  it("omits sessions when only one session is returned", async () => {
    const target = await retiredInvocationRedirect(
      "inv-1",
      {},
      { getInvocation: async () => makeInvocation(["sess-1"]) },
    );
    expect(target.search.sessions).toBeUndefined();
  });

  it("redirects to /fleet with the invocation id when there are zero sessions", async () => {
    const target = await retiredInvocationRedirect(
      "inv-1",
      { project: "ocean" },
      { getInvocation: async () => makeInvocation([]) },
    );
    expect(target).toEqual({
      to: "/fleet",
      search: { project: "ocean", invocation: "inv-1" },
    });
  });

  it("preserves incoming search alongside the resolved session", async () => {
    const target = await retiredInvocationRedirect(
      "inv-1",
      { status: "completed" },
      { getInvocation: async () => makeInvocation(["sess-1"]) },
    );
    expect(target.search.status).toBe("completed");
  });

  it("rejects when the invocation fetch rejects, instead of swallowing the error", async () => {
    await expect(
      retiredInvocationRedirect(
        "inv-1",
        {},
        {
          getInvocation: async () => {
            throw new Error("backend detail: invocation not found");
          },
        },
      ),
    ).rejects.toThrow("backend detail: invocation not found");
  });
});

// ─── formatRetiredRouteError ────────────────────────────────────────────────

describe("formatRetiredRouteError", () => {
  it("returns an Error's message", () => {
    expect(formatRetiredRouteError(new Error("backend detail"))).toBe("backend detail");
  });

  it("returns a plain string error as-is", () => {
    expect(formatRetiredRouteError("boom")).toBe("boom");
  });

  it("falls back to a generic message for unrecognized error shapes", () => {
    expect(formatRetiredRouteError({})).toBe("This link could not be resolved.");
    expect(formatRetiredRouteError(undefined)).toBe("This link could not be resolved.");
  });
});

// ─── Route source-contract: retired routes exist and use the shared helper ──

const ROUTES_DIR = path.resolve(__dirname, "../routes");

describe("retired route files use the consolidated helper", () => {
  const RETIRED_ROUTES = [
    "playfield/index.tsx",
    "runs/index.tsx",
    "runs/$id.tsx",
    "invocations/index.tsx",
    "invocations/$id.tsx",
    "kanban/index.tsx",
    "skills/index.tsx",
    "plugins/index.tsx",
    "engines/index.tsx",
    "playbooks/index.tsx",
    "playbooks/new/index.tsx",
    "playbooks/$name/index.tsx",
    "playbooks/$name/edit/index.tsx",
  ];

  for (const file of RETIRED_ROUTES) {
    it(`${file} validates search through preserveRetiredSearch`, () => {
      const src = fs.readFileSync(path.join(ROUTES_DIR, file), "utf-8");
      expect(src).toMatch(/validateSearch:\s*preserveRetiredSearch/);
    });
  }

  it("the invocation detail route defines an errorComponent", () => {
    const src = fs.readFileSync(path.join(ROUTES_DIR, "invocations/$id.tsx"), "utf-8");
    expect(src).toMatch(/errorComponent/);
  });

  it("the invocation detail route does not catch the redirect helper's rejection", () => {
    const src = fs.readFileSync(path.join(ROUTES_DIR, "invocations/$id.tsx"), "utf-8");
    expect(src).not.toMatch(/catch/);
  });
});

// ─── Route tree: generated codegen actually includes the new routes ─────────

describe("routeTree.gen.ts includes every retired-route path", () => {
  const ROUTE_TREE_SRC = fs.readFileSync(path.resolve(__dirname, "../routeTree.gen.ts"), "utf-8");

  for (const routePath of [
    "/playfield/",
    "/runs/",
    "/runs/$id",
    "/invocations/",
    "/invocations/$id",
  ]) {
    it(`registers ${routePath}`, () => {
      expect(ROUTE_TREE_SRC).toContain(`path: '${routePath}'`);
    });
  }
});
