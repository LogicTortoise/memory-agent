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
    baseURL: "http://127.0.0.1:8420",
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