import { defineConfig } from 'vitest/config'
import type { Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { TanStackRouterVite } from '@tanstack/router-plugin/vite'
import path from 'path'

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
function hostedApiBaseInjector(): Plugin {
  const onVercel = !!process.env.VERCEL
  const base = process.env.STUDIO_HOSTED_API_BASE ?? 'http://127.0.0.1:8765'
  return {
    name: 'studio-hosted-api-base',
    apply: 'build',
    transformIndexHtml() {
      if (!onVercel) return
      return [
        {
          tag: 'script',
          children: `window.__STUDIO_API_BASE__=${JSON.stringify(base)};`,
          injectTo: 'head-prepend',
        },
      ]
    },
  }
}

// The e2e harness points this at a seeded daemon on a dynamically-allocated
// free port (see e2e/global-setup.ts); everyone else keeps the default 8765.
// 127.0.0.1, not localhost: the daemon binds IPv4 explicitly, and on hosts
// where localhost resolves to ::1 first, proxying to it would fail even
// though preview itself (bound to --host 127.0.0.1) is healthy.
// STUDIO_API_URL overrides the whole target (e.g. an isolated dev daemon).
const apiTarget =
  process.env.STUDIO_API_URL ?? `http://127.0.0.1:${process.env.STUDIO_E2E_API_PORT ?? '8765'}`

export default defineConfig({
  plugins: [
    TanStackRouterVite({
      routesDirectory: './src/routes',
      generatedRouteTree: './src/routeTree.gen.ts',
    }),
    react(),
    hostedApiBaseInjector(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': apiTarget,
    },
  },
  // `vite preview` serves the production build the same way the e2e harness
  // does: a single static origin proxying /api server-side, so the browser
  // never needs CORS and resolveApiBase() picks up the same-origin ("") path.
  preview: {
    proxy: {
      '/api': apiTarget,
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    // e2e/ holds Playwright specs (see playwright.config.ts) -- a different
    // test runner with its own test()/expect(), never vitest's.
    exclude: ['e2e/**', 'node_modules/**'],
  },
})
