import { expect, test } from "@playwright/test";

// Screenshots against the LIVE stack (UI on :3000, API on :8000) so the numbers are real, not fixtures.
// Run: LIVE_EMAIL=... LIVE_PASSWORD=... npx playwright test e2e/live-screens.spec.ts --workers=1
// Skipped when the credentials are not set. The credentials are read from the environment only and never stored.
const BASE = process.env.LIVE_BASE_URL ?? "http://localhost:3000";
const PAGES: [string, string][] = [
  ["command-center", "/"],
  ["population", "/population"],
  ["insights", "/insights"],
  ["residents", "/residents"],
  ["reports", "/reports"],
  ["programs", "/programs"],
  ["journey", "/journey"],
  ["operations", "/operations"],
  ["care-gaps", "/care-gaps"],
  ["evidence-review", "/evidence-review"],
  ["guidelines", "/guidelines"],
  ["guideline-studio", "/guidelines/studio"],
  ["planning", "/planning"],
  ["engagement", "/engagement"],
  ["evaluation", "/evaluation"],
  ["monitor", "/monitor"],
  ["jobs", "/jobs"],
  ["audit", "/audit"],
  ["integrations", "/integrations"],
];

test.skip(!process.env.LIVE_EMAIL || !process.env.LIVE_PASSWORD, "set LIVE_EMAIL and LIVE_PASSWORD to capture live screenshots");
test.use({ viewport: { width: 1440, height: 900 }, baseURL: BASE });

test("live screenshots of the owned pages", async ({ page }) => {
  test.setTimeout(900_000);
  await page.goto("/login");
  await page.getByLabel(/email/i).fill(process.env.LIVE_EMAIL ?? "");
  await page.getByLabel(/password/i).fill(process.env.LIVE_PASSWORD ?? "");
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await page.waitForTimeout(4000);
  await page.screenshot({ path: "test-results/screenshots/live-after-login.png" });
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible({ timeout: 30_000 });
  const only = process.env.LIVE_ONLY?.split(",");
  for (const [name, path] of PAGES.filter(([n]) => !only || only.includes(n))) {
    await page.goto(path);
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible({ timeout: 60_000 });
    await expect(page.locator(".skeleton")).toHaveCount(0, { timeout: 60_000 }).catch(() => undefined);
    await page.waitForTimeout(1800);
    await page.screenshot({ path: `test-results/screenshots/live-${name}.png` });
  }
});
