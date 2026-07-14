/**
 * vercel.json SPA-rewrite path-shape check.
 *
 * Vercel's `rewrites[].source` is matched by path-to-regexp, not by plain
 * RegExp — the assertions here compile the pattern's literal text as a
 * RegExp, which approximates but does not exactly reproduce path-to-regexp
 * semantics (the negative-lookahead `(?!assets/)` used below happens to be
 * valid in both). Good enough to lock the *intent*: hashed asset requests
 * fall through to their static file, every other path gets the SPA shell.
 */

import { describe, it, expect, vi } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

vi.mock("@tanstack/router-plugin/vite", () => ({
  TanStackRouterVite: () => ({ name: "router-test-plugin" }),
}));

import { hostedApiBaseInjector } from "../../vite.config.mjs";

const vercelConfig = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, "../../vercel.json"), "utf-8"),
) as { rewrites: { source: string; destination: string }[] };

describe("vercel.json SPA rewrite pattern (RegExp approximation of path-to-regexp)", () => {
  const rewrite = vercelConfig.rewrites[0];
  const pattern = new RegExp(`^${rewrite.source}$`);

  it("rewrites to index.html", () => {
    expect(rewrite.destination).toBe("/index.html");
  });

  it("does not match paths under /assets/ (those must fall through to the static file)", () => {
    expect(pattern.test("/assets/index-abc123.js")).toBe(false);
    expect(pattern.test("/assets/index-abc123.css")).toBe(false);
    expect(pattern.test("/assets/deeply/nested/chunk.js")).toBe(false);
  });

  it("matches app routes so the SPA shell is served for client-side routing", () => {
    expect(pattern.test("/")).toBe(true);
    expect(pattern.test("/fleet")).toBe(true);
    expect(pattern.test("/schedules")).toBe(true);
    expect(pattern.test("/runs/run-0000000000000001")).toBe(true);
  });

  it("still matches a path that merely contains 'assets/' past the first segment", () => {
    // The negative lookahead only guards the START of the path; this is the
    // documented approximation gap between a plain RegExp and path-to-regexp
    // route matching, not a claim that this is the desired behavior.
    expect(pattern.test("/docs/assets/foo")).toBe(true);
  });
});

describe("hosted API base injection", () => {
  function transformFor(env: NodeJS.ProcessEnv) {
    const transform = hostedApiBaseInjector(env).transformIndexHtml as () => unknown;
    return transform();
  }

  it("prepends the loopback API-base override to hosted builds", () => {
    expect(transformFor({ VERCEL: "1" })).toEqual([
      {
        tag: "script",
        children: 'window.__STUDIO_API_BASE__="http://127.0.0.1:8765";',
        injectTo: "head-prepend",
      },
    ]);
  });

  it("injects nothing into plain builds", () => {
    expect(transformFor({})).toBeUndefined();
  });
});
