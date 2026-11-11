import { defineConfig, devices } from "@playwright/test";

const PORT = 3100;

// Tests never touch real Supabase or FastAPI: the browser's calls are mocked with page.route.
// The Next.js server itself still needs a Supabase URL, so point it at a closed local port
// (connection refused is instant) with a dummy publishable key.
export const TEST_SUPABASE_URL = "http://localhost:54321";

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results/artifacts", // screenshots live in test-results/screenshots and survive reruns
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
    reuseExistingServer: false,
    env: {
      NEXT_PUBLIC_SUPABASE_URL: TEST_SUPABASE_URL,
      NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_test_only",
      ANTICIPA_API_ORIGIN: "http://localhost:9", // unreachable on purpose: any un-mocked /api call fails loudly
    },
  },
});
