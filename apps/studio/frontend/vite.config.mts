import { defineConfig } from "vitest/config";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import fs from "node:fs";
import path from "node:path";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// The hosted Studio (lion-studio.khive.ai and any Vercel preview) is a static,
// local-first deploy: its origin has no API of its own, the page talks to the
// operator's own `li studio` daemon on loopback. resolveApiBase() otherwise
// falls through to same-origin — correct for a single-origin Docker/reverse-proxy
// build, wrong here, where same-origin /api/* hits the SPA rewrite and returns
// index.html. So on Vercel builds ONLY (VERCEL=1 is set by every Vercel build,
// Git-integration or CLI), inject the loopback base as the highest-priority
// runtime override. Gating on VERCEL keeps source/Docker single-origin builds
// (which run the same `vite build`) on their correct same-origin default.
// Loopback http from an https page is exempt from mixed-content blocking.
export function hostedApiBaseInjector(env: NodeJS.ProcessEnv = process.env): Plugin {
  const onVercel = env.VERCEL === "1";
  const base = env.STUDIO_HOSTED_API_BASE ?? "http://127.0.0.1:8765";
  return {
    name: "studio-hosted-api-base",
    apply: "build",
    transformIndexHtml() {
      if (!onVercel) return;
      return [
        {
          tag: "script",
          children: `window.__STUDIO_API_BASE__=${JSON.stringify(base)};`,
          injectTo: "head-prepend",
        },
      ];
    },
  };
}

// The hosted Studio fronts page analytics with a self-hosted Umami tracker.
// The tracker tag is injected ONLY when STUDIO_UMAMI_WEBSITE_ID is set in the
// build environment (the Vercel project env for lion-studio.khive.ai), so local,
// source, Docker, and packaged builds ship no tag and never contact the analytics
// host. Gating on the variable rather than VERCEL also keeps a preview or a
// self-hosted static build from reporting unless it explicitly opts in.
//
// Two tags ride ahead of the tracker, in this order:
//   1. the inline before-send hook (analytics/before-send.js), a classic script
//      that scrubs ids, search strings, and hashes out of every payload and
//      drops identify calls and custom events;
//   2. the deferred tracker tag, pointed at the hook by data-before-send and
//      restricted to the hosted domain by data-domains.
// The hook must be defined before the tracker runs, so it is injected first and
// synchronously; the tracker is deferred. A malformed website id fails the build
// here instead of reaching an HTML attribute.
export function hostedAnalyticsInjector(env: NodeJS.ProcessEnv = process.env): Plugin {
  const websiteId = env.STUDIO_UMAMI_WEBSITE_ID;
  if (websiteId && !UUID_RE.test(websiteId)) {
    throw new Error(`STUDIO_UMAMI_WEBSITE_ID must be a UUID, got ${JSON.stringify(websiteId)}`);
  }
  return {
    name: "studio-hosted-analytics",
    apply: "build",
    transformIndexHtml() {
      if (!websiteId) return;
      const hookSource = fs.readFileSync(
        path.resolve(__dirname, "analytics/before-send.js"),
        "utf-8",
      );
      return [
        {
          tag: "script",
          children: hookSource,
          injectTo: "head",
        },
        {
          tag: "script",
          attrs: {
            defer: true,
            src: "https://analytics.khive.ai/script.js",
            "data-website-id": websiteId,
            "data-domains": "lion-studio.khive.ai",
            "data-exclude-search": "true",
            "data-exclude-hash": "true",
            "data-before-send": "__studioAnalyticsBeforeSend",
          },
          injectTo: "head",
        },
      ];
    },
  };
}

// The e2e harness points this at a seeded daemon on a dynamically-allocated
// free port (see e2e/global-setup.ts); everyone else keeps the default 8765.
// 127.0.0.1, not localhost: the daemon binds IPv4 explicitly, and on hosts
// where localhost resolves to ::1 first, proxying to it would fail even
// though preview itself (bound to --host 127.0.0.1) is healthy.
// STUDIO_API_URL overrides the whole target (e.g. an isolated dev daemon).
const apiTarget =
  process.env.STUDIO_API_URL ?? `http://127.0.0.1:${process.env.STUDIO_E2E_API_PORT ?? "8765"}`;

export const studioProxy = {
  "/api": apiTarget,
  "/health": apiTarget,
  "/openapi.json": apiTarget,
};

export default defineConfig({
  plugins: [
    TanStackRouterVite({
      routesDirectory: "./src/routes",
      generatedRouteTree: "./src/routeTree.gen.ts",
      autoCodeSplitting: true,
    }),
    react(),
    hostedApiBaseInjector(),
    hostedAnalyticsInjector(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: studioProxy,
  },
  // `vite preview` serves the production build the same way the e2e harness
  // does: a single static origin proxying /api server-side, so the browser
  // never needs CORS and resolveApiBase() picks up the same-origin ("") path.
  preview: {
    proxy: studioProxy,
  },
  build: {
    // Keep two large, independently stable payload families out of the shell.
    // Broad node_modules chunking creates circular React/Zustand imports under
    // Rolldown; these exact boundaries have one-way dependencies and preserve
    // route-level splitting.
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "locale",
              test: /src[\\/]messages[\\/]/,
              maxSize: 100_000,
              priority: 2,
            },
            {
              name: "markdown",
              test: /node_modules[\\/]react-markdown[\\/]/,
              priority: 1,
            },
          ],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // e2e/ holds Playwright specs (see playwright.config.ts) -- a different
    // test runner with its own test()/expect(), never vitest's.
    exclude: ["e2e/**", "node_modules/**"],
    // Pins the process timezone so any test asserting a rendered clock value
    // (e.g. runLabel's local-time disambiguator) gets the same digits on
    // every machine and in CI, instead of only passing in the author's TZ.
    env: { TZ: "UTC" },
  },
});
