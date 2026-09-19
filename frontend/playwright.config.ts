import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end config for the two-browser realtime tests.
 *
 * These run against the *already running* dev stack (API on 8001, app on 3001)
 * rather than starting their own, because the point of the test is to exercise
 * the real Postgres, the real Redis and the real WebSocket - not a mock of any
 * of them.
 */
export default defineConfig({
  testDir: "./e2e",
  // Realtime assertions are about propagation between two contexts, so a
  // serial run keeps the timing legible and the failures honest.
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3001",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
