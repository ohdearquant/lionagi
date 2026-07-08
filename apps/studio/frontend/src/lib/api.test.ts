import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { resolveApiBase, resolveAuthToken } from "./api";

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
