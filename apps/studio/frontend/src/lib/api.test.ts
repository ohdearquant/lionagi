import { describe, it, expect, beforeEach, vi } from "vitest";
import { resolveApiBase } from "./api";

describe("resolveApiBase", () => {
  beforeEach(() => {
    // Reset any window overrides between tests
    delete (window as Window & { __STUDIO_API_BASE__?: string }).__STUDIO_API_BASE__;
  });

  it("returns window.__STUDIO_API_BASE__ when set", () => {
    (window as Window & { __STUDIO_API_BASE__?: string }).__STUDIO_API_BASE__ =
      "http://custom-host:9000";
    expect(resolveApiBase()).toBe("http://custom-host:9000");
  });

  it("ignores empty __STUDIO_API_BASE__", () => {
    (window as Window & { __STUDIO_API_BASE__?: string }).__STUDIO_API_BASE__ = "";
    const result = resolveApiBase();
    // Falls through to env or default — must not return empty string
    expect(result).toBeTruthy();
    expect(result).not.toBe("");
  });

  it("falls back to default when no overrides are set", () => {
    // No window override, no VITE env, port is jsdom default (no port = '')
    const result = resolveApiBase();
    expect(result).toBe("http://localhost:8765");
  });

  it("returns default for same port 8765", () => {
    // Simulate window.location.port === '8765' — should still return default
    vi.stubGlobal("window", {
      ...window,
      __STUDIO_API_BASE__: undefined,
      location: { ...window.location, port: "8765", protocol: "http:" },
    });
    const result = resolveApiBase();
    expect(result).toBe("http://localhost:8765");
    vi.unstubAllGlobals();
  });
});
