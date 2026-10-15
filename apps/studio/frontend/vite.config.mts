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
// The tracker is injected ONLY into the hosted Vercel build: the build must set
// VERCEL=1 (the same signal hostedApiBaseInjector above reads, set by every
// Vercel build) AND STUDIO_UMAMI_WEBSITE_ID (the Vercel project env for
// lion-studio.khive.ai) to a UUID. Both gates matter. VERCEL alone would cover
// any Vercel deploy, and the id alone would cover the desktop build: the Tauri
// shell loads this same dist/ (its tauri.conf.json frontendDist points at it),
// its page holds the desktop's per-launch auth token, and its CSP is null, so a
// packaged build that exported the id from its environment would ship the
// tracker into the authenticated desktop document. Local, source, Docker,
// desktop, and packaged builds therefore carry no tag and never contact the
// analytics host. Vite does not copy `.env*` files into the config's process
// environment, so setting the variable in an `.env` file does not turn the
// tracker on; the build process itself must export it.
//
// One inline script is injected, containing the before-send hook
// (analytics/before-send.js) followed by a loader (analytics/load-tracker.js)
// that is called with the tracker's attributes as a JSON literal. The hook
// rebuilds each payload from an allowlist of page-view fields and drops every
// other send type; the loader then appends the async tracker tag, pointed at
// the hook by data-before-send and restricted to the hosted domain by
// data-domains, only when the hook is present. The tracker therefore loads only
// after the hook exists: if the inline script did not run, no tag is added and
// no raw path or page title is sent. The tag carries the Subresource Integrity
// pin of the tracker build the host serves plus crossorigin="anonymous" (the
// host answers with `access-control-allow-origin: *`, which an anonymous
// integrity check needs), so a changed or replaced script fails the integrity
// check and does not run. The pinned copy of that build lives beside this file
// at analytics/umami-script.pinned.js, for the test that checks the pin against
// it; the build never loads it. When the host's tracker changes, update the
// pinned copy and TRACKER_INTEGRITY together, or the browser refuses the script.
// A malformed website id, or a hook or loader whose text is not the expected
// shape, fails the build here instead of reaching the page.
export const TRACKER_INTEGRITY =
  "sha384-KovSIPpdrAZNHs+M91d7FOrLat5rqcpTtQUq/GLIzYwAt+eN0EQHlgdUgm/0U2j+";

type AnalyticsSources = {
  hook: string;
  loader: string;
};

function readAnalyticsSources(): AnalyticsSources {
  return {
    hook: fs.readFileSync(path.resolve(__dirname, "analytics/before-send.js"), "utf-8"),
    loader: fs.readFileSync(path.resolve(__dirname, "analytics/load-tracker.js"), "utf-8"),
  };
}

export function hostedAnalyticsInjector(
  env: NodeJS.ProcessEnv = process.env,
  sources: AnalyticsSources = readAnalyticsSources(),
): Plugin {
  const websiteId = env.STUDIO_UMAMI_WEBSITE_ID;
  if (websiteId && !UUID_RE.test(websiteId)) {
    throw new Error(
      `STUDIO_UMAMI_WEBSITE_ID must be a UUID; received a value of length ${websiteId.length}`,
    );
  }
  // Fail closed on a script whose text is not the shape the injected inline
  // script relies on: the hook must be a complete statement list ending in a
  // semicolon, and the loader must be a single parenthesized IIFE so the call
  // appended below joins the same expression.
  if (!/;\s*$/.test(sources.hook)) {
    throw new Error("analytics/before-send.js must end in a semicolon");
  }
  const loaderSource = sources.loader.replace(/;\s*$/, "");
  if (!/\)$/.test(loaderSource)) {
    throw new Error("analytics/load-tracker.js must end in a parenthesized function expression");
  }
  return {
    name: "studio-hosted-analytics",
    apply: "build",
    transformIndexHtml() {
      if (env.VERCEL !== "1" || !websiteId) return;
      const trackerAttrs = {
        src: "https://analytics.khive.ai/script.js",
        integrity: TRACKER_INTEGRITY,
        crossorigin: "anonymous",
        "data-website-id": websiteId,
        "data-domains": "lion-studio.khive.ai",
        "data-exclude-search": "true",
        "data-exclude-hash": "true",
        "data-before-send": "__studioAnalyticsBeforeSend",
      };
      return [
        {
          tag: "script",
          children: `${sources.hook}\n${loaderSource}(${JSON.stringify(trackerAttrs)});`,
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
