import { defineConfig, devices } from "@playwright/test";

// Config for the judge-facing feature specs (e2e/features-*.spec.ts). Own port and Next dist dir so it never touches the
// owner's dev server (:3000) or the design-system suite (:3100). The browser's API calls are route-mocked.
const PORT = 3200;

export default defineConfig({
  testDir: "./e2e",
  testMatch: /features-.*\.spec\.ts/,
  outputDir: "./test-results/artifacts-b",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: 2,
  reporter: [["list"]],
  use: { baseURL: `http://localhost:${PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: `npx next dev -p ${PORT}`,
    url: `http://localhost:${PORT}/login`,
    timeout: 180_000,
    reuseExistingServer: true,
    env: {
      NEXT_DIST_DIR: ".next/e2e-b",
      NEXT_PUBLIC_SUPABASE_URL: "http://localhost:54321",
      NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_test_only",
      ANTICIPA_API_ORIGIN: "http://localhost:9", // unreachable on purpose: an un-mocked /api call fails loudly
    },
  },
});
