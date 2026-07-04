import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { TanStackRouterVite } from '@tanstack/router-plugin/vite'
import path from 'path'

// The e2e harness points this at a seeded daemon on a dynamically-allocated
// free port (see e2e/global-setup.ts); everyone else keeps the default 8765.
const apiTarget = `http://localhost:${process.env.STUDIO_E2E_API_PORT ?? '8765'}`

export default defineConfig({
  plugins: [
    TanStackRouterVite({
      routesDirectory: './src/routes',
      generatedRouteTree: './src/routeTree.gen.ts',
    }),
    react(),
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
