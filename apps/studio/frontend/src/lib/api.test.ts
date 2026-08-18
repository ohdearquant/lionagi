import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { normalizeSignalEvent, resolveApiBase, resolveAuthToken } from "./api";
import type { SignalEvent } from "./api";

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

  it("uses the same-origin Vite proxy on dev port 3000", () => {
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
    expect(result).toBe("");
  });

  it("uses the same-origin Vite proxy on dev port 5173", () => {
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
    expect(result).toBe("");
  });

  it("keeps a remotely accessed dev server on its same-origin proxy", () => {
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
    expect(result).toBe("");
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

  it("is same-origin for HTTPS on a non-local hostname (single-origin Docker deploy)", () => {
    // https://server.example — Docker/reverse-proxy deployment serving the
    // SPA and API from one origin behind TLS termination.
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "",
        hostname: "server.example",
        protocol: "https:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("");
  });

  it("still honors an explicit runtime override for a hosted-static deploy on HTTPS", () => {
    // https://lion-studio.khive.ai — static SPA driving a separate local
    // daemon must opt in explicitly rather than relying on a hostname guess.
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: "http://127.0.0.1:8765",
      location: {
        ...window.location,
        port: "",
        hostname: "lion-studio.khive.ai",
        protocol: "https:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("http://127.0.0.1:8765");
  });

  it("does not apply the hosted-https default over plain http on a remote hostname", () => {
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "",
        hostname: "lion-studio.khive.ai",
        protocol: "http:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("");
  });

  it("does not apply the hosted-https default for https on localhost", () => {
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: {
        ...window.location,
        port: "",
        hostname: "localhost",
        protocol: "https:",
      },
    });
    const result = resolveApiBase();
    expect(result).toBe("");
  });

  describe("VITE_STUDIO_API_BASE (build-time env)", () => {
    afterEach(() => {
      vi.unstubAllEnvs();
    });

    it("uses VITE_STUDIO_API_BASE when no runtime override is set", () => {
      vi.stubEnv("VITE_STUDIO_API_BASE", "https://api.hosted.example");
      vi.stubGlobal("window", {
        ...window,
        __STUDIO_API_BASE__: undefined,
        location: { ...window.location, port: "", hostname: "server.example", protocol: "https:" },
      });
      expect(resolveApiBase()).toBe("https://api.hosted.example");
    });

    it("ignores an empty VITE_STUDIO_API_BASE and falls through to origin logic", () => {
      vi.stubEnv("VITE_STUDIO_API_BASE", "");
      vi.stubGlobal("window", {
        ...window,
        __STUDIO_API_BASE__: undefined,
        location: { ...window.location, port: "", hostname: "server.example", protocol: "http:" },
      });
      expect(resolveApiBase()).toBe("");
    });

    it("prefers the runtime override over VITE_STUDIO_API_BASE when both are set", () => {
      vi.stubEnv("VITE_STUDIO_API_BASE", "https://api.hosted.example");
      vi.stubGlobal("window", {
        ...window,
        __STUDIO_API_BASE__: "http://127.0.0.1:8765",
        location: { ...window.location, port: "", hostname: "server.example", protocol: "https:" },
      });
      expect(resolveApiBase()).toBe("http://127.0.0.1:8765");
    });
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

describe("engine run summary transport", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses the canonical list route and carries the opaque cursor", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify({ version: 1, items: [], next_cursor: null }), {
          status: 200,
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { listEngineRuns } = await import("./api");

    const page = await listEngineRuns({ limit: 20, cursor: "opaque-cursor" });

    expect(page).toEqual({ version: 1, items: [], next_cursor: null });
    expect(String(fetchMock.mock.calls[0]?.[0])).toMatch(
      /\/api\/engine-runs\/\?limit=20&cursor=opaque-cursor$/,
    );
  });

  it("reveals a redacted spec only when explicitly requested", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response(JSON.stringify({ id: "run-1" }), { status: 200 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { getEngineRun } = await import("./api");

    await getEngineRun("run-1");
    await getEngineRun("run-1", { includeSpec: true });

    expect(String(fetchMock.mock.calls[0]?.[0])).toMatch(/\/api\/engine-runs\/run-1$/);
    expect(String(fetchMock.mock.calls[1]?.[0])).toMatch(
      /\/api\/engine-runs\/run-1\?include_spec=true$/,
    );
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

describe("fetchJson HTML-fallback / no-backend guard", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: { ...window.location, port: "", hostname: "server.example", protocol: "http:" },
    });
  });

  it("throws a clear error when the SPA rewrite returns index.html for an /api/* path", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response("<!doctype html><html><body>app</body></html>", {
            status: 200,
            headers: { "Content-Type": "text/html" },
          }),
        ),
      ),
    );

    const { getStats } = await import("./api");
    await expect(getStats()).rejects.toThrow(/returned HTML instead of JSON/);
    await expect(getStats()).rejects.toThrow(/VITE_STUDIO_API_BASE/);
  });

  it("still parses a normal JSON response with an explicit content-type", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ playbooks: 3, agents: 1 }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );

    const { getStats } = await import("./api");
    const result = await getStats();
    expect(result).toEqual({ playbooks: 3, agents: 1 });
  });

  it("still parses a JSON response when no content-type header is present (existing behavior)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ playbooks: 5 }), { status: 200 }))),
    );

    const { getStats } = await import("./api");
    const result = await getStats();
    expect(result).toEqual({ playbooks: 5 });
  });

  it("returns undefined for a 204 No Content response instead of throwing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))),
    );

    const { deleteEngineDef } = await import("./api");
    const result = await deleteEngineDef("def-1");
    expect(result).toBeUndefined();
  });
});

describe("fetchJson validation-error messages", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: { ...window.location, port: "", hostname: "server.example", protocol: "http:" },
    });
  });

  function stub422(detail: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ detail }), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
  }

  it("names the field and the reason for a validation error instead of the status code", async () => {
    stub422([{ type: "missing", loc: ["body", "instruction"], msg: "Field required", input: {} }]);

    const { getStats } = await import("./api");
    await expect(getStats()).rejects.toThrow(/instruction/);
    await expect(getStats()).rejects.toThrow(/Field required/);
    await expect(getStats()).rejects.not.toThrow(/Request failed/);
  });

  it("joins several validation errors and reports the field path of each", async () => {
    stub422([
      { type: "missing", loc: ["body", "prompt"], msg: "Field required" },
      {
        type: "string_type",
        loc: ["body", "opts", "effort"],
        msg: "Input should be a valid string",
      },
    ]);

    const { getStats } = await import("./api");
    await expect(getStats()).rejects.toThrow(/prompt: Field required/);
    await expect(getStats()).rejects.toThrow(/opts\.effort: Input should be a valid string/);
  });

  it("caps a long validation list rather than rendering an unreadable wall of text", async () => {
    stub422(
      Array.from({ length: 7 }, (_, i) => ({
        type: "missing",
        loc: ["body", `field${i}`],
        msg: "Field required",
      })),
    );

    const { getStats } = await import("./api");
    // The first few are named; the rest are counted, so the message stays readable.
    await expect(getStats()).rejects.toThrow(/field0: Field required/);
    await expect(getStats()).rejects.toThrow(/\+4 more/);
    await expect(getStats()).rejects.not.toThrow(/field6/);
  });

  it("falls back to the status code when the array carries nothing readable", async () => {
    // A non-Pydantic array body must not produce a confident-looking empty message.
    stub422([1, 2, 3]);

    const { getStats } = await import("./api");
    await expect(getStats()).rejects.toThrow(/Request failed: 422/);
  });

  it("still prefers a plain string detail (unchanged behavior)", async () => {
    stub422("that playbook is already running");

    const { getStats } = await import("./api");
    await expect(getStats()).rejects.toThrow(/that playbook is already running/);
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

// ─── Runs/sessions query construction (Fleet filters, cursor pagination) ─────

describe("runs/sessions query construction", () => {
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

  it("listRuns sends search and project_null (project_null wins over project)", async () => {
    const calls = stubFetch({
      runs: [],
      page: 1,
      per_page: 20,
      total: 0,
      total_pages: 0,
      has_next: false,
      has_prev: false,
    });
    const { listRuns } = await import("./api");
    await listRuns({ search: "50%off", project: "org/alpha", project_null: true });
    const url = new URL(calls[0]!.url, "http://localhost");
    expect(url.searchParams.get("search")).toBe("50%off");
    expect(url.searchParams.get("project_null")).toBe("true");
    expect(url.searchParams.has("project")).toBe(false);
  });

  it("listRuns sends project when project_null is not set", async () => {
    const calls = stubFetch({
      runs: [],
      page: 1,
      per_page: 20,
      total: 0,
      total_pages: 0,
      has_next: false,
      has_prev: false,
    });
    const { listRuns } = await import("./api");
    await listRuns({ project: "org/alpha" });
    const url = new URL(calls[0]!.url, "http://localhost");
    expect(url.searchParams.get("project")).toBe("org/alpha");
    expect(url.searchParams.has("project_null")).toBe(false);
  });

  it("listRunProjects — GET /api/runs/projects", async () => {
    const payload = { projects: [{ project: "org/alpha", count: 3, last_activity: 1 }], total: 3 };
    const calls = stubFetch(payload);
    const { listRunProjects } = await import("./api");
    const result = await listRunProjects();
    expect(result).toEqual(payload);
    expect(calls[0]?.url).toMatch(/\/api\/runs\/projects/);
  });

  it("getSession sends message_cursor, not message_offset, when a cursor is given", async () => {
    const calls = stubFetch({ id: "s1", name: "s", created_at: 1, updated_at: 1, branches: [] });
    const { getSession } = await import("./api");
    await getSession("s1", { messageCursor: "cursor-1", messageLimit: 50 });
    const url = new URL(calls[0]!.url, "http://localhost");
    expect(url.searchParams.get("message_cursor")).toBe("cursor-1");
    expect(url.searchParams.get("message_limit")).toBe("50");
    expect(url.searchParams.has("message_offset")).toBe(false);
  });

  it("getSession omits message_cursor entirely when not given (first page)", async () => {
    const calls = stubFetch({ id: "s1", name: "s", created_at: 1, updated_at: 1, branches: [] });
    const { getSession } = await import("./api");
    await getSession("s1");
    const url = new URL(calls[0]!.url, "http://localhost");
    expect(url.searchParams.has("message_cursor")).toBe(false);
  });
});

// The backend stamps a signal's `ts` with time.time() — Unix SECONDS — and the
// frontend compares it against Date.now(), which is milliseconds. The gap is
// about 1.7 trillion, so an un-normalized fresh event reads as ancient rather
// than as malformed: nothing throws, and every node carrying a signal quietly
// reports itself stalled. Converting once here is what keeps every consumer
// from having to know where the number came from.
describe("normalizeSignalEvent", () => {
  const raw: SignalEvent = {
    id: "e1",
    session_id: "s1",
    seq: 1,
    kind: "NodeStarted",
    op_id: "op1",
    ts: 1_770_000_000,
    payload: { name: "plan" },
  };

  it("converts the backend's seconds into epoch milliseconds", () => {
    expect(normalizeSignalEvent(raw).ts).toBe(1_770_000_000_000);
  });

  it("leaves every other field alone", () => {
    const out = normalizeSignalEvent(raw);
    expect({ ...out, ts: raw.ts }).toEqual(raw);
  });

  it("does not mutate the event it was given", () => {
    normalizeSignalEvent(raw);
    expect(raw.ts).toBe(1_770_000_000);
  });

  it("lands on the same clock Date.now() reports", () => {
    // The assertion that actually matters: a timestamp taken now, stamped the
    // way the backend stamps it, must come back within a second of Date.now()
    // — and the un-normalized value must NOT, or this conversion is decorative.
    const now = Date.now();
    const asBackendSends = { ...raw, ts: now / 1000 };

    expect(Math.abs(normalizeSignalEvent(asBackendSends).ts - now)).toBeLessThan(1000);
    expect(Math.abs(asBackendSends.ts - now)).toBeGreaterThan(1_000_000_000);
  });
});
