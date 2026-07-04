import { describe, it, expect, beforeEach, vi } from "vitest";
import { resolveApiBase, resolveAuthToken } from "./api";
import type { AgentProfile, WorkerFormData } from "./types";

describe("resolveApiBase", () => {
  beforeEach(() => {
    // Reset any window overrides between tests
    delete (window as Window & { __STUDIO_API_BASE__?: string }).__STUDIO_API_BASE__;
    vi.unstubAllGlobals();
  });

  it("returns window.__STUDIO_API_BASE__ when set", () => {
    (window as Window & { __STUDIO_API_BASE__?: string }).__STUDIO_API_BASE__ =
      "http://custom-host:9000";
    expect(resolveApiBase()).toBe("http://custom-host:9000");
  });

  it("ignores empty __STUDIO_API_BASE__", () => {
    (window as Window & { __STUDIO_API_BASE__?: string }).__STUDIO_API_BASE__ = "";
    const result = resolveApiBase();
    // Falls through to same-origin ("") for jsdom (no port) — must not be the override value
    expect(result).not.toBe("http://custom-host:9000");
  });

  it("returns same-origin empty string when no overrides and no port (production)", () => {
    // jsdom default: window.location.port === '' — production same-origin deployment
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: { ...window.location, port: "", hostname: "server.example", protocol: "http:" },
    });
    const result = resolveApiBase();
    expect(result).toBe("");
  });

  it("returns same-origin empty string for port 8765 (single-origin production)", () => {
    // Browser opened http://server.example:8765 — SPA and API on same origin
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "8765",
        hostname: "server.example",
        protocol: "http:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("");
  });

  it("returns hostname:8765 for Vite dev port 3000", () => {
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "3000",
        hostname: "localhost",
        protocol: "http:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("http://localhost:8765");
  });

  it("returns hostname:8765 for Vite dev port 5173", () => {
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "5173",
        hostname: "localhost",
        protocol: "http:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("http://localhost:8765");
  });

  it("uses actual hostname (not hardcoded localhost) for dev port with remote host", () => {
    // Dev server accessed from a different machine or container
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "3000",
        hostname: "dev.example.com",
        protocol: "http:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("http://dev.example.com:8765");
  });

  it("same-origin for non-localhost host on port 8765", () => {
    // Docker/remote deployment: http://192.0.2.10:8765 → same origin
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "8765",
        hostname: "192.0.2.10",
        protocol: "http:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("");
  });
});

describe("resolveAuthToken", () => {
  beforeEach(() => {
    delete (window as Window & { __STUDIO_AUTH_TOKEN__?: string }).__STUDIO_AUTH_TOKEN__;
    vi.unstubAllGlobals();
  });

  it("returns undefined when token is not set", () => {
    expect(resolveAuthToken()).toBeUndefined();
  });

  it("returns the token when __STUDIO_AUTH_TOKEN__ is set", () => {
    (window as Window & { __STUDIO_AUTH_TOKEN__?: string }).__STUDIO_AUTH_TOKEN__ =
      "deadbeef0102030405060708090a0b0c";
    expect(resolveAuthToken()).toBe("deadbeef0102030405060708090a0b0c");
  });

  it("returns undefined for empty string token", () => {
    (window as Window & { __STUDIO_AUTH_TOKEN__?: string }).__STUDIO_AUTH_TOKEN__ = "";
    expect(resolveAuthToken()).toBeUndefined();
  });
});

describe("fetchJson Authorization header", () => {
  beforeEach(() => {
    delete (window as Window & { __STUDIO_AUTH_TOKEN__?: string }).__STUDIO_AUTH_TOKEN__;
    vi.unstubAllGlobals();
  });

  it("attaches Authorization header when token is present", async () => {
    (window as Window & { __STUDIO_AUTH_TOKEN__?: string }).__STUDIO_AUTH_TOKEN__ =
      "abc123token456";

    const captured: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        captured.push(init ?? {});
        return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      }),
    );

    // Import fetchJson indirectly via a public wrapper. getStats() calls fetchJson internally.
    const { getStats } = await import("./api");
    await getStats();

    expect(captured.length).toBeGreaterThan(0);
    const headers = captured[0]?.headers as Record<string, string> | undefined;
    expect(headers?.["Authorization"]).toBe("Bearer abc123token456");
  });

  it("does not attach Authorization header when token is absent", async () => {
    const captured: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        captured.push(init ?? {});
        return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      }),
    );

    const { getStats } = await import("./api");
    await getStats();

    expect(captured.length).toBeGreaterThan(0);
    const headers = captured[0]?.headers as Record<string, string> | undefined;
    expect(headers?.["Authorization"]).toBeUndefined();
  });
});

describe("engine defs API", () => {
  type FetchCall = { url: string; init?: RequestInit };

  function stubFetch(response: unknown, status = 200): FetchCall[] {
    const calls: FetchCall[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve(
          new Response(JSON.stringify(response), {
            status,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
    return calls;
  }

  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: { ...window.location, port: "8765", hostname: "localhost", protocol: "http:" },
    });
  });

  it("listEngineDefs — GET /api/engine-defs/ with no params", async () => {
    const payload = [{ id: "abc", name: "My Engine", kind: "research" }];
    const calls = stubFetch(payload);
    const { listEngineDefs } = await import("./api");
    const result = await listEngineDefs();
    expect(result).toEqual(payload);
    expect(calls[0]?.url).toMatch(/\/api\/engine-defs\//);
    expect(calls[0]?.init?.method).toBeUndefined(); // GET
  });

  it("listEngineDefs — appends kind query param when provided", async () => {
    const calls = stubFetch([]);
    const { listEngineDefs } = await import("./api");
    await listEngineDefs({ kind: "coding" });
    expect(calls[0]?.url).toMatch(/[?&]kind=coding/);
  });

  it("getEngineDef — GET /api/engine-defs/:id", async () => {
    const def = { id: "def-1", name: "Coder", kind: "coding" };
    const calls = stubFetch(def);
    const { getEngineDef } = await import("./api");
    const result = await getEngineDef("def-1");
    expect(result).toEqual(def);
    expect(calls[0]?.url).toMatch(/\/api\/engine-defs\/def-1/);
  });

  it("getEngineDef — URL-encodes the id", async () => {
    const calls = stubFetch({ id: "x y", name: "X Y", kind: "review" });
    const { getEngineDef } = await import("./api");
    await getEngineDef("x y");
    expect(calls[0]?.url).toContain("x%20y");
  });

  it("createEngineDef — POST /api/engine-defs/ with body", async () => {
    const response = { id: "new-id", name: "Research Bot", created_at: 1234567890 };
    const calls = stubFetch(response, 200);
    const { createEngineDef } = await import("./api");
    const result = await createEngineDef({ name: "Research Bot", kind: "research" });
    expect(result).toEqual(response);
    expect(calls[0]?.init?.method).toBe("POST");
    expect((calls[0]?.init?.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    const body = JSON.parse(calls[0]?.init?.body as string);
    expect(body.name).toBe("Research Bot");
    expect(body.kind).toBe("research");
  });

  it("updateEngineDef — PUT /api/engine-defs/:id with body", async () => {
    const calls = stubFetch({ ok: true });
    const { updateEngineDef } = await import("./api");
    const result = await updateEngineDef("def-1", { model: "claude-opus-4-5" });
    expect(result).toEqual({ ok: true });
    expect(calls[0]?.init?.method).toBe("PUT");
    expect((calls[0]?.init?.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    expect(calls[0]?.url).toMatch(/\/api\/engine-defs\/def-1/);
    const body = JSON.parse(calls[0]?.init?.body as string);
    expect(body.model).toBe("claude-opus-4-5");
  });

  it("deleteEngineDef — DELETE /api/engine-defs/:id", async () => {
    const calls = stubFetch({ ok: true });
    const { deleteEngineDef } = await import("./api");
    const result = await deleteEngineDef("def-1");
    expect(result).toEqual({ ok: true });
    expect(calls[0]?.init?.method).toBe("DELETE");
    expect(calls[0]?.url).toMatch(/\/api\/engine-defs\/def-1/);
  });

  it("launchEngine — POST /api/launches/ with action_kind=engine", async () => {
    const response = {
      invocation_id: "inv-1",
      action_kind: "engine",
    };
    const calls = stubFetch(response, 202);
    const { launchEngine } = await import("./api");
    const result = await launchEngine({
      action_kind: "engine",
      action_engine_def: "def-1",
      action_prompt: "build a crawler",
    });
    expect(result).toEqual(response);
    expect(calls[0]?.init?.method).toBe("POST");
    expect(calls[0]?.url).toMatch(/\/api\/launches\//);
    expect((calls[0]?.init?.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    const body = JSON.parse(calls[0]?.init?.body as string);
    expect(body.action_kind).toBe("engine");
    expect(body.action_engine_def).toBe("def-1");
    expect(body.action_prompt).toBe("build a crawler");
  });
});

describe("exact API path pinning (zero 307 redirects)", () => {
  // The daemon mounts every route at the exact path its @studio_route decorator
  // declares (lionagi/studio/services/*.py), including trailing-slash exactness.
  // A request whose path doesn't match exactly gets a 307 redirect before it
  // reaches the handler — browsers silently follow it (an extra round trip),
  // but a dev proxy that only forwards the literal path it was asked for does
  // not, so the request dead-ends. Each case below pins one api.ts call to the
  // exact path and method the daemon registers, so a future edit that drifts
  // a path fails here instead of shipping a silent redirect hop.
  type ApiModule = typeof import("./api");

  function stubFetch(): { url: string; init?: RequestInit }[] {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve(
          new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
        );
      }),
    );
    return calls;
  }

  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: { ...window.location, port: "8765", hostname: "localhost", protocol: "http:" },
    });
  });

  const agentProfile: AgentProfile = {
    name: "a1",
    path: "/tmp/a1",
    provider: "anthropic",
    model: "claude",
    system_prompt: null,
    guidance: null,
  };

  const workerFormData: WorkerFormData = {
    name: "pb1",
    description: "",
    use: { models: {} },
    steps: {},
    links: [],
  };

  const cases: Array<{
    name: string;
    method: string;
    path: string;
    call: (api: ApiModule) => Promise<unknown>;
  }> = [
    { name: "listRuns()", method: "GET", path: "/api/runs/", call: (api) => api.listRuns() },
    {
      name: "listRuns(project, status)",
      method: "GET",
      path: "/api/runs/?project=demo&status=running",
      call: (api) => api.listRuns({ project: "demo", status: ["running"] }),
    },
    { name: "getRun", method: "GET", path: "/api/runs/r1", call: (api) => api.getRun("r1") },
    {
      name: "listWorkers",
      method: "GET",
      path: "/api/playbooks/",
      call: (api) => api.listWorkers(),
    },
    {
      name: "getWorkerGraph",
      method: "GET",
      path: "/api/playbooks/pb1",
      call: (api) => api.getWorkerGraph("pb1"),
    },
    {
      name: "createWorker",
      method: "POST",
      path: "/api/playbooks/pb1",
      call: (api) => api.createWorker("pb1", workerFormData),
    },
    {
      name: "updateWorker",
      method: "PUT",
      path: "/api/playbooks/pb1",
      call: (api) => api.updateWorker("pb1", workerFormData),
    },
    {
      name: "validateWorker",
      method: "POST",
      path: "/api/playbooks/pb1/validate",
      call: (api) => api.validateWorker("pb1", workerFormData),
    },
    {
      name: "updatePlaybook",
      method: "PUT",
      path: "/api/playbooks/pb1",
      call: (api) => api.updatePlaybook("pb1", {}),
    },
    {
      name: "startRun",
      method: "POST",
      path: "/api/playbooks/pb1/run",
      call: (api) => api.startRun("pb1"),
    },
    { name: "listAgents", method: "GET", path: "/api/agents/", call: (api) => api.listAgents() },
    { name: "getAgent", method: "GET", path: "/api/agents/a1", call: (api) => api.getAgent("a1") },
    {
      name: "createAgent",
      method: "POST",
      path: "/api/agents/a1",
      call: (api) => api.createAgent("a1", agentProfile),
    },
    {
      name: "updateAgent",
      method: "PUT",
      path: "/api/agents/a1",
      call: (api) => api.updateAgent("a1", agentProfile),
    },
    { name: "listShows", method: "GET", path: "/api/shows/", call: (api) => api.listShows() },
    { name: "getShow", method: "GET", path: "/api/shows/t1", call: (api) => api.getShow("t1") },
    {
      name: "listSessions",
      method: "GET",
      path: "/api/sessions/",
      call: (api) => api.listSessions(),
    },
    {
      name: "getSession",
      method: "GET",
      path: "/api/sessions/s1",
      call: (api) => api.getSession("s1"),
    },
    {
      name: "getArtifact",
      method: "GET",
      path: "/api/artifacts/a1",
      call: (api) => api.getArtifact("a1"),
    },
    {
      name: "listArtifactsForSession",
      method: "GET",
      path: "/api/artifacts/by-session/s1",
      call: (api) => api.listArtifactsForSession("s1"),
    },
    {
      name: "listInvocations()",
      method: "GET",
      path: "/api/invocations/",
      call: (api) => api.listInvocations(),
    },
    {
      name: "listInvocations(skill)",
      method: "GET",
      path: "/api/invocations/?skill=review",
      call: (api) => api.listInvocations({ skill: "review" }),
    },
    {
      name: "getInvocation",
      method: "GET",
      path: "/api/invocations/i1",
      call: (api) => api.getInvocation("i1"),
    },
    {
      name: "listDefinitions()",
      method: "GET",
      path: "/api/definitions/",
      call: (api) => api.listDefinitions(),
    },
    {
      name: "listDefinitions(kind)",
      method: "GET",
      path: "/api/definitions/?kind=agent",
      call: (api) => api.listDefinitions("agent"),
    },
    {
      name: "getDefinition",
      method: "GET",
      path: "/api/definitions/agent/n1",
      call: (api) => api.getDefinition("agent", "n1"),
    },
    {
      name: "getDefinitionVersion",
      method: "GET",
      path: "/api/definitions/agent/n1/versions/2",
      call: (api) => api.getDefinitionVersion("agent", "n1", 2),
    },
    {
      name: "saveDefinition",
      method: "POST",
      path: "/api/definitions/agent/n1",
      call: (api) => api.saveDefinition("agent", "n1", "content"),
    },
    {
      name: "rollbackDefinition",
      method: "POST",
      path: "/api/definitions/agent/n1/rollback?version=2",
      call: (api) => api.rollbackDefinition("agent", "n1", 2),
    },
    {
      name: "snapshotDefinitions()",
      method: "POST",
      path: "/api/definitions/snapshot",
      call: (api) => api.snapshotDefinitions(),
    },
    {
      name: "snapshotDefinitions(kind)",
      method: "POST",
      path: "/api/definitions/snapshot?kind=agent",
      call: (api) => api.snapshotDefinitions("agent"),
    },
    { name: "listSkills", method: "GET", path: "/api/skills/", call: (api) => api.listSkills() },
    { name: "getSkill", method: "GET", path: "/api/skills/s1", call: (api) => api.getSkill("s1") },
    {
      name: "listPlugins",
      method: "GET",
      path: "/api/plugins",
      call: (api) => api.listPlugins(),
    },
    {
      name: "getPlugin",
      method: "GET",
      path: "/api/plugins/p1",
      call: (api) => api.getPlugin("p1"),
    },
    {
      name: "getPluginSkill",
      method: "GET",
      path: "/api/plugins/p1/skills/sk1",
      call: (api) => api.getPluginSkill("p1", "sk1"),
    },
    {
      name: "getAdminDoctor",
      method: "GET",
      path: "/api/admin/doctor",
      call: (api) => api.getAdminDoctor(),
    },
    {
      name: "pruneAdmin",
      method: "POST",
      path: "/api/admin/prune",
      call: (api) => api.pruneAdmin({}),
    },
    {
      name: "runMaintenance",
      method: "POST",
      path: "/api/admin/maintenance",
      call: (api) => api.runMaintenance("vacuum"),
    },
    {
      name: "listProjects",
      method: "GET",
      path: "/api/projects/",
      call: (api) => api.listProjects(),
    },
    {
      name: "getProject",
      method: "GET",
      path: "/api/projects/p1",
      call: (api) => api.getProject("p1"),
    },
    {
      name: "createProject",
      method: "POST",
      path: "/api/projects/",
      call: (api) => api.createProject({ name: "p1" }),
    },
    {
      name: "updateProject",
      method: "PUT",
      path: "/api/projects/p1",
      call: (api) => api.updateProject("p1", {}),
    },
    {
      name: "deleteProject",
      method: "DELETE",
      path: "/api/projects/p1",
      call: (api) => api.deleteProject("p1"),
    },
    { name: "listTeams()", method: "GET", path: "/api/teams/", call: (api) => api.listTeams() },
    {
      name: "listTeams(limit)",
      method: "GET",
      path: "/api/teams/?limit=5",
      call: (api) => api.listTeams({ limit: 5 }),
    },
    { name: "getTeam", method: "GET", path: "/api/teams/t1", call: (api) => api.getTeam("t1") },
    { name: "getStats", method: "GET", path: "/api/stats", call: (api) => api.getStats() },
    {
      name: "listSchedules()",
      method: "GET",
      path: "/api/schedules/",
      call: (api) => api.listSchedules(),
    },
    {
      name: "getSchedule",
      method: "GET",
      path: "/api/schedules/sc1",
      call: (api) => api.getSchedule("sc1"),
    },
    {
      name: "createSchedule",
      method: "POST",
      path: "/api/schedules/",
      call: (api) => api.createSchedule({}),
    },
    {
      name: "updateSchedule",
      method: "PATCH",
      path: "/api/schedules/sc1",
      call: (api) => api.updateSchedule("sc1", {}),
    },
    {
      name: "deleteSchedule",
      method: "DELETE",
      path: "/api/schedules/sc1",
      call: (api) => api.deleteSchedule("sc1"),
    },
    {
      name: "enableSchedule",
      method: "POST",
      path: "/api/schedules/sc1/enable",
      call: (api) => api.enableSchedule("sc1"),
    },
    {
      name: "disableSchedule",
      method: "POST",
      path: "/api/schedules/sc1/disable",
      call: (api) => api.disableSchedule("sc1"),
    },
    {
      name: "triggerSchedule",
      method: "POST",
      path: "/api/schedules/sc1/trigger",
      call: (api) => api.triggerSchedule("sc1"),
    },
    {
      name: "listScheduleRuns",
      method: "GET",
      path: "/api/schedules/sc1/runs",
      call: (api) => api.listScheduleRuns("sc1"),
    },
    {
      name: "listEngineRuns()",
      method: "GET",
      path: "/api/engine-runs/",
      call: (api) => api.listEngineRuns(),
    },
    {
      name: "getEngineRun",
      method: "GET",
      path: "/api/engine-runs/er1",
      call: (api) => api.getEngineRun("er1"),
    },
    {
      name: "listEngineDefs",
      method: "GET",
      path: "/api/engine-defs/",
      call: (api) => api.listEngineDefs(),
    },
    {
      name: "getEngineDef",
      method: "GET",
      path: "/api/engine-defs/ed1",
      call: (api) => api.getEngineDef("ed1"),
    },
    {
      name: "createEngineDef",
      method: "POST",
      path: "/api/engine-defs/",
      call: (api) => api.createEngineDef({ name: "n", kind: "k" }),
    },
    {
      name: "updateEngineDef",
      method: "PUT",
      path: "/api/engine-defs/ed1",
      call: (api) => api.updateEngineDef("ed1", {}),
    },
    {
      name: "deleteEngineDef",
      method: "DELETE",
      path: "/api/engine-defs/ed1",
      call: (api) => api.deleteEngineDef("ed1"),
    },
    {
      name: "launchEngine",
      method: "POST",
      path: "/api/launches/",
      call: (api) =>
        api.launchEngine({
          action_kind: "engine",
          action_engine_def: "ed1",
          action_prompt: "p",
        }),
    },
  ];

  it.each(cases)("$name -> $method $path", async ({ method, path, call }) => {
    const calls = stubFetch();
    const api = await import("./api");
    await call(api);
    expect(calls[0]?.url).toBe(path);
    const actualMethod = (calls[0]?.init?.method as string | undefined)?.toUpperCase() ?? "GET";
    expect(actualMethod).toBe(method);
  });
});
