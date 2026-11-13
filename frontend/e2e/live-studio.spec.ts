import { expect, test } from "@playwright/test";

// Guideline Studio dry run against the LIVE stack (UI :3000, API :8000, worker running).
// Run: LIVE_EMAIL=... LIVE_PASSWORD=... npx playwright test e2e/live-studio.spec.ts --workers=1 --trace off
// Skipped without credentials; the credentials are read from the environment only.
test.skip(!process.env.LIVE_EMAIL || !process.env.LIVE_PASSWORD, "set LIVE_EMAIL and LIVE_PASSWORD");
test.use({ viewport: { width: 1440, height: 900 }, baseURL: process.env.LIVE_BASE_URL ?? "http://localhost:3000" });

test("a dry run started from the Studio finishes with results", async ({ page }) => {
  test.setTimeout(300_000);
  const calls: string[] = [];
  page.on("response", (r) => {
    if (/dry-run/.test(r.url())) calls.push(`${r.request().method()} ${r.status()}`);
  });
  await page.goto("/login");
  await page.getByLabel(/email/i).fill(process.env.LIVE_EMAIL ?? "");
  await page.getByLabel(/password/i).fill(process.env.LIVE_PASSWORD ?? "");
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible({ timeout: 60_000 });
  await page.goto("/guidelines/studio");
  const run = page.getByRole("button", { name: /^Run dry run$/ });
  await expect(run).toBeVisible({ timeout: 60_000 });
  await expect(run).toBeEnabled({ timeout: 30_000 });
  await run.click();
  const results = page.getByTestId("dry-run-results");
  const failed = page.getByText(/The dry run did not (finish|start)/);
  await expect(results.or(failed)).toBeVisible({ timeout: 200_000 });
  await page.screenshot({ path: "test-results/screenshots/live-studio-dryrun.png", fullPage: true });
  console.log("DRYRUN_CALLS", calls.join(" | "));
  await expect(failed).toHaveCount(0);
  await expect(results).toBeVisible();
});
