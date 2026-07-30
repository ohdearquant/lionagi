import { describe, expect, it } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const html = fs.readFileSync(path.resolve(__dirname, "../../index.html"), "utf-8");
const bootstrapMatch = html.match(/<script id="studio-connection-bootstrap">([\s\S]*?)<\/script>/);
if (!bootstrapMatch) throw new Error("connection bootstrap script not found");
const bootstrap = bootstrapMatch[1];
const analyticsMatch = html.match(/<script id="studio-analytics-bootstrap">([\s\S]*?)<\/script>/);
if (!analyticsMatch) throw new Error("analytics bootstrap script not found");
const analyticsBootstrap = analyticsMatch[1];

function executeBootstrap(options: {
  hash: string;
  initial?: Record<string, string>;
  storageBlocked?: boolean;
}) {
  const values = new Map(Object.entries(options.initial ?? {}));
  const replaced: string[] = [];
  const fakeWindow = {
    location: {
      hash: options.hash,
      pathname: "/",
      search: "?view=fleet",
    },
    history: {
      state: { preserved: true },
      replaceState: (_state: unknown, _title: string, url: string) => replaced.push(url),
    },
  } as unknown as Window;
  const storage = {
    setItem(key: string, value: string) {
      if (options.storageBlocked) throw new Error("blocked");
      values.set(key, value);
    },
    getItem(key: string) {
      if (options.storageBlocked) throw new Error("blocked");
      return values.get(key) ?? null;
    },
  };
  const run = new Function(
    "window",
    "document",
    "sessionStorage",
    "URLSearchParams",
    "URL",
    bootstrap,
  );
  run(fakeWindow, { title: "Lion Studio" }, storage, URLSearchParams, URL);
  return { fakeWindow, values, replaced };
}

describe("pre-module Studio connection bootstrap", () => {
  it("runs before analytics and the main module", () => {
    const bootstrapIndex = html.indexOf('id="studio-connection-bootstrap"');
    expect(bootstrapIndex).toBeGreaterThan(0);
    expect(bootstrapIndex).toBeLessThan(html.indexOf("analytics.khive.ai"));
    expect(bootstrapIndex).toBeLessThan(html.indexOf('type="module"'));
  });

  it("never injects third-party analytics into an authenticated control-plane tab", () => {
    expect(analyticsBootstrap).toContain("if (window.__STUDIO_AUTH_TOKEN__) return");
    const appended: unknown[] = [];
    const run = new Function("window", "document", analyticsBootstrap);
    run(
      { __STUDIO_AUTH_TOKEN__: "sensitive" },
      {
        createElement: () => {
          throw new Error("analytics must not be created");
        },
        head: { appendChild: (node: unknown) => appended.push(node) },
      },
    );
    expect(appended).toEqual([]);

    const created: Record<string, unknown> = {
      setAttribute(name: string, value: string) {
        this[name] = value;
      },
    };
    run(
      {},
      {
        createElement: () => created,
        head: { appendChild: (node: unknown) => appended.push(node) },
      },
    );
    expect(appended).toEqual([created]);
    expect(created.src).toBe("https://analytics.khive.ai/script.js");
  });

  it("hydrates globals, persists the tab session, and scrubs credentials", () => {
    const { fakeWindow, values, replaced } = executeBootstrap({
      hash: "#studio-api=http%3A%2F%2F127.0.0.1%3A8765&studio-human-token=secret&keep=yes",
    });
    expect(fakeWindow.__STUDIO_API_BASE__).toBe("http://127.0.0.1:8765");
    expect(fakeWindow.__STUDIO_AUTH_TOKEN__).toBe("secret");
    expect(values.get("studio-api")).toBe("http://127.0.0.1:8765");
    expect(values.get("studio-token")).toBe("secret");
    expect(replaced).toEqual(["/?view=fleet#keep=yes"]);
    expect(replaced[0]).not.toContain("secret");
  });

  it("restores on reload and still initializes when storage is blocked", () => {
    const restored = executeBootstrap({
      hash: "",
      initial: {
        "studio-api": "http://127.0.0.1:9999",
        "studio-token": "restored-token",
      },
    });
    expect(restored.fakeWindow.__STUDIO_API_BASE__).toBe("http://127.0.0.1:9999");
    expect(restored.fakeWindow.__STUDIO_AUTH_TOKEN__).toBe("restored-token");

    const blocked = executeBootstrap({
      hash: "#apiBase=http%3A%2F%2F127.0.0.1%3A8765&humanToken=one-load-token",
      storageBlocked: true,
    });
    expect(blocked.fakeWindow.__STUDIO_API_BASE__).toBe("http://127.0.0.1:8765");
    expect(blocked.fakeWindow.__STUDIO_AUTH_TOKEN__).toBe("one-load-token");
    expect(blocked.replaced).toEqual(["/?view=fleet"]);
  });

  it("never pairs a partial or non-loopback fragment with a stored bearer", () => {
    const priorPair = {
      "studio-api": "http://127.0.0.1:8765",
      "studio-token": "prior-token",
    };
    const malicious = executeBootstrap({
      hash: "#studio-api=https%3A%2F%2Fevil.example",
      initial: priorPair,
    });
    expect(malicious.fakeWindow.__STUDIO_API_BASE__).toBe("http://127.0.0.1:8765");
    expect(malicious.fakeWindow.__STUDIO_AUTH_TOKEN__).toBe("prior-token");
    expect(malicious.fakeWindow.__STUDIO_API_BASE__).not.toContain("evil.example");
    expect(malicious.replaced).toEqual(["/?view=fleet"]);

    const partialLoopback = executeBootstrap({
      hash: "#studio-api=http%3A%2F%2Flocalhost%3A9999",
      initial: priorPair,
    });
    expect(partialLoopback.fakeWindow.__STUDIO_API_BASE__).toBe("http://127.0.0.1:8765");
    expect(partialLoopback.fakeWindow.__STUDIO_AUTH_TOKEN__).toBe("prior-token");
  });
});
