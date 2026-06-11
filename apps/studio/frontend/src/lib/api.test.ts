import { describe, it, expect, beforeEach, vi } from "vitest";
import { resolveApiBase } from "./api";

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
