import { defineConfig, devices } from "@playwright/test";

// Smoke-only scaffold: the UI is being redesigned in parallel, so this
// deliberately stays shallow (boot + one API round-trip + direct-nav checks)
// against a seeded daemon (see e2e/global-setup.ts and tests/e2e_studio/).
export default defineConfig({
  testDir: "./e2e",
  timeout: 15_000,
  globalTimeout: 5 * 60_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  globalSetup: "./e2e/global-setup.ts",
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
