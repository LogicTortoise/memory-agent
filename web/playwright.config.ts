import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests-e2e",
  testMatch: ["**/*.e2e.test.{ts,tsx}"],
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 8_000 },
  use: {
    // Default port is 8440 (clear of the local Octopus service on
    // 8420). Operators running the UI on a different port can override
    // via MEMORY_AGENT_PLAYWRIGHT_BASE_URL or by passing
    // --baseURL to `npx playwright test`. See docs/22-v1-p7-p8.
    baseURL: "http://127.0.0.1:8440",
    trace: "off",
    screenshot: "only-on-failure",
    video: "off",
    // Bypass the "trusted event" check so key navigation works inside
    // narrow-viewport tests.
    ignoreHTTPSErrors: true,
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "narrow",
      use: { ...devices["Desktop Chrome"], viewport: { width: 480, height: 800 } },
    },
  ],
});