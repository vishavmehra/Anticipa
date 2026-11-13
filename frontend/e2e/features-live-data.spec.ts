import { existsSync, readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { ok } from "./helpers";

// Renders the feature screens with REAL read model payloads captured from the live database (read only, no writes) for one
// AI cohort resident. Skipped unless LIVE_FIXTURES points at the capture file written by the capture script. The browser's
// API calls are still route-mocked, so this checks shapes and wording of real data, not the network.
const file = process.env.LIVE_FIXTURES;
const live = file && existsSync(file) ? JSON.parse(readFileSync(file, "utf-8")) : null;

test.describe("real payloads", () => {
  test.skip(!live, "LIVE_FIXTURES is not set");

  const handlers = () => ({
    [`GET /residents/${live.resident_id}/journey`]: () => ({ body: ok(live.journey) }),
    [`GET /residents/${live.resident_id}/pedigree`]: () => ({ body: ok(live.pedigree) }),
    "GET /engagement/preview": () => ({ body: ok(live.preview) }),
    "GET /analytics/funnel": () => ({ body: ok(live.funnel) }),
  });

  test("journey, explainer, pedigree and preview render the real resident without errors or dashes", async ({ page, context }) => {
    await bootB(page, context, "observer", handlers());
    await page.goto(`/residents/${live.resident_id}/journey`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Journey of resident");
    const stepper = page.getByRole("navigation", { name: "Resident journey stages" });
    await expect(stepper.getByRole("button")).toHaveCount(5);
    await page.screenshot({ path: "test-results/screenshots/live-journey-act.png", fullPage: true });
    await page.getByRole("button", { name: /^Decide/ }).click();
    await expect(page.getByTestId("why-due")).toBeVisible();
    await expectNoDashes(page);
    await page.screenshot({ path: "test-results/screenshots/live-journey-decide.png", fullPage: true });
    await page.getByRole("button", { name: /^Read/ }).click();
    await expectNoDashes(page);
    await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveCount(0);
  });
});
