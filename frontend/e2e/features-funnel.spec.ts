import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes, funnelPayload } from "./features-fixtures";
import { evalRun, evalRunsList } from "./features-fixtures-eval";
import { commandCenter } from "./features-fixtures-ref";
import { capacityOverview, careGapTotals, guidelinesList, programmesPayload } from "./features-fixtures-ref2";
import { ok, paged } from "./helpers";

// The funnel sits at the bottom of the Impact tab of the Programs and impact page.
const host = {
  "GET /guidelines": () => ({ body: paged(guidelinesList()) }),
  "GET /analytics/programmes": () => ({ body: ok(programmesPayload()) }),
  "GET /care-gaps": (_r: unknown, url: URL) => ({ body: paged(careGapTotals(url).rows, 1, 1, careGapTotals(url).total) }),
  "GET /analytics/command-center": () => ({ body: ok(commandCenter()) }),
  "GET /analytics/capacity": () => ({ body: ok(capacityOverview()) }),
  "GET /analytics/evaluation": () => ({ body: ok(evalRun()) }),
  "GET /analytics/evaluation/runs": () => ({ body: paged(evalRunsList()) }),
};

// Population funnel (feature 4), hosted on /evaluation.
const WIDTHS = [
  { w: 1440, h: 900 },
  { w: 390, h: 844 },
] as const;

test("funnel shows seven live stages with definitions, conversion and leakage", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...host, "GET /analytics/funnel": () => ({ body: ok(funnelPayload()) }) });
  await page.goto("/evaluation");
  const list = page.getByRole("list", { name: "Care gap funnel stages" });
  await expect(list.getByRole("listitem")).toHaveCount(7);
  await expect(list.locator('[data-stage="residents"]')).toContainText("200,000");
  await expect(list.locator('[data-stage="due"]')).toContainText("30,000");
  await expect(list.locator('[data-stage="due"]')).toContainText("20% of the stage above");
  await expect(list.locator('[data-stage="due"]')).toContainText("Distinct residents whose latest evaluation");
  await expect(list.locator('[data-stage="closed"]')).toContainText("900");
  await expect(page.getByText("Left the funnel without completion")).toBeVisible();
  await expect(page.getByText("Escalated: 75")).toBeVisible();
  await expect(page.getByText("Live", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await page.getByRole("region", { name: "Care gap funnel" }).getByText("View as table").click();
  await expect(page.getByRole("table", { name: "Care gap funnel" }).getByRole("row")).toHaveCount(8);
  await expectNoDashes(page);
});

test("funnel says so when the endpoint is missing and shows the error with its request id otherwise", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...host, "GET /analytics/funnel": () => ({ status: 404, body: { error: { code: "NOT_FOUND", message: "nope", details: [], request_id: "req-1" } } }) });
  await page.goto("/evaluation");
  await expect(page.getByText("Endpoint not yet available")).toBeVisible();
});

for (const { w, h } of WIDTHS) {
  test(`funnel at ${w}px: screenshot, axe, no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: h });
    await bootB(page, context, "observer", { ...host, "GET /analytics/funnel": () => ({ body: ok(funnelPayload()) }) });
    await page.goto("/evaluation");
    await expect(page.getByRole("list", { name: "Care gap funnel stages" }).getByRole("listitem")).toHaveCount(7);
    await page.screenshot({ path: `test-results/screenshots/features-funnel-${w}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}
