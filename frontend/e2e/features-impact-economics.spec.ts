import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { impactReport } from "./features-fixtures-impact";
import { popHandlers } from "./features-fixtures-pop";
import { ok } from "./helpers";

// Impact and economics tab of /evaluation. Backend call route mocked; values are invented for the test.
const handlers = { ...popHandlers(0), "GET /analytics/impact": () => ({ body: ok(impactReport()) }) };

test("impact and economics: figures, denominators, method notes and honest limits", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/evaluation");
  await page.getByRole("tab", { name: "Impact and economics" }).click();
  const kpis = page.getByRole("region", { name: "Impact figures" });
  await expect(kpis).toContainText("1,829");
  await expect(kpis).toContainText("of 10,000 residents whose notes were read (18%)");
  await expect(kpis).toContainText("52,352");
  await expect(kpis).toContainText("of 86,988 care gaps (60%)");
  await expect(kpis).toContainText("1.1 points");
  await expect(kpis).toContainText("down from 12.9 points");
  await expect(kpis).toContainText("$0.0027");
  const notes = page.getByRole("region", { name: /Reading notes finds care gaps/ });
  await expect(notes).toContainText("211 of 577");
  await expect(notes).toContainText("562 of 577");
  await expect(notes).toContainText("269 of 4,754");
  await expect(notes).toContainText("3 of 4,754");
  await expect(notes).toContainText("1,497 screening decisions");
  await expect(page.getByRole("list", { name: "Care gaps opened only because notes were read, by guideline" })).toContainText("Genetic counselling referral");
  const plan = page.getByRole("table", { name: "First come first served compared with the optimised plan" });
  await expect(plan).toContainText("12.9 points");
  await expect(plan).toContainText("1.1 points");
  await expect(page.getByText("The optimiser changes who gets a seat, not how many.")).toBeVisible();
  await expect(page.getByText("Per resident found that records miss")).toBeVisible();
  await expect(page.getByText(/Staff hours saved are not estimated/)).toBeVisible();
  await page.getByText("How this is measured").first().click();
  await expect(page.getByText(/the approved guideline engine runs twice/)).toBeVisible();
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expect(page.getByText(/simulated/i)).toHaveCount(0);
  await expect(page.getByText(/Residents, notes and clinic capacity are generated/)).toBeVisible();
  await expect(page.getByText(/Capacity is modelled/)).toBeVisible();
  await expect(page.getByRole("region", { name: "How each figure is defined" })).toContainText("min group size");
  await expect(page.getByText(/pending clinical review/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.screenshot({ path: "test-results/screenshots/ref-impact-economics.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-impact-economics-full.png", fullPage: true });
});

test("impact and economics explains a missing report", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...popHandlers(0), "GET /analytics/impact": () => ({ status: 404, body: { error: { code: "NOT_FOUND", message: "no impact report yet", details: [], request_id: "r" } } }) });
  await page.goto("/evaluation");
  await page.getByRole("tab", { name: "Impact and economics" }).click();
  await expect(page.getByText("No impact report has been produced yet")).toBeVisible();
});

for (const w of [1440, 390]) {
  test(`impact and economics at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await bootB(page, context, "observer", handlers);
    await page.goto("/evaluation");
    await page.getByRole("tab", { name: "Impact and economics" }).click();
    await expect(page.getByRole("region", { name: "Impact figures" })).toBeVisible();
    await expect(page.locator(".skeleton")).toHaveCount(0, { timeout: 15_000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}
