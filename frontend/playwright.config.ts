/**
 * Playwright config for the Execution Console smoke suite.
 *
 * The suite exercises the **mock-mode** end-to-end flow only —
 * upload → run-detail → SSE timeline → terminal close. Mock mode
 * keeps the test hermetic: no backend / Temporal / file system
 * dependencies, no flaky networks, deterministic event timing.
 *
 * Live-mode integration tests live (or will live) under a separate
 * config that brings up the docker compose stack — they need the
 * full FastAPI + Temporal worker + ingestion pipeline to be up,
 * which costs minutes per run and isn't what `npm run e2e` should
 * pay every time.
 *
 * Tests live under `e2e/` to keep them off the unit-test glob
 * (`vitest.config.ts` only picks up `src/**\/*.test.ts`).
 */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // Each spec file owns its own browser context; we don't need
  // parallelism to make the smoke suite fast (it's a few hundred
  // ms per spec).
  fullyParallel: true,
  reporter: process.env.CI ? "line" : "list",
  retries: process.env.CI ? 1 : 0,

  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },

  // Run Vite's dev server before the suite. We use the same dev
  // server `npm run dev` starts, with a longer `timeout` to absorb
  // npm-cache cold starts in CI.
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: "ignore",
    stderr: "pipe",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
