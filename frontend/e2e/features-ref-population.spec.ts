import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { DUE_TOTAL, explorerPayload, popHandlers } from "./features-fixtures-pop";
import { ok } from "./helpers";

// Population, Insights and Reports (references: Anticipa India Population Health Dashboard, Population Insights Lab,
// Reports & Insights Dashboard). All backend calls are route mocked; nothing here is real data.

const axeScan = (page: import("@playwright/test").Page) =>
  new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
const fmtViolations = (v: Awaited<ReturnType<typeof axeScan>>["violations"]) => v.map((x) => `${x.id}: ${x.nodes.map((n) => n.target.join(" ")).join(" | ")}`);

test("population: scope totals, programme coverage, pyramid and equity by due share", async ({ page, context }) => {
  await bootB(page, context, "observer", popHandlers(0));
  await page.goto("/population");
  await expect(page.getByRole("heading", { level: 1, name: "Population Health Intelligence" })).toBeVisible();
  const glance = page.getByRole("region", { name: "Population at a glance" });
  await expect(glance).toContainText("4 of 36");
  await expect(glance).toContainText("15"); // facilities
  await expect(glance).toContainText("Residents with complete records");
  await expect(glance).toContainText("Residents in linked families");
  await expect(glance).toContainText("Families");
  for (const jargon of ["full clinical detail", "rule-relevant", "directed rows", "Family edges", "Constructed", "death date", "recorded death"]) await expect(glance).not.toContainText(jargon);
  await glance.getByRole("button", { name: "What Families means" }).hover();
  await expect(page.getByRole("tooltip").filter({ hasText: "linked together as one family" })).toBeVisible();
  await expect(glance).toContainText("Households are not recorded");
  await expect(page.getByText("No earlier period yet").first()).toBeVisible();
  const prog = page.getByRole("region", { name: "Key programmes" });
  await expect(prog).toContainText("Breast cancer screening");
  await expect(prog).toContainText("71%"); // 30,000 of 42,000
  const pyramid = page.getByRole("list", { name: "Population pyramid by age band and sex" });
  await expect(pyramid).toBeVisible();
  const band = pyramid.getByRole("button", { name: "50 to 64" });
  await band.click();
  await expect(band).toHaveAttribute("aria-pressed", "true");
  const equity = page.getByRole("region", { name: /Equity and access/ });
  await equity.getByRole("button", { name: "Due for care" }).click();
  await expect(equity).toContainText(DUE_TOTAL.toLocaleString("en-US"));
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expect(page.getByText("District placement is not available")).toHaveCount(0);
  await expectNoDashes(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);
  await page.mouse.move(1000, 150);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(600);
  await page.screenshot({ path: "test-results/screenshots/ref-population.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-population-full.png", fullPage: true });
});

test("population shows the change since the period before when there is one", async ({ page, context }) => {
  await bootB(page, context, "observer", popHandlers(20000));
  await page.goto("/population");
  await expect(page.getByText("+3,000 since the period before").first()).toBeVisible();
  await expect(page.getByText(/Infinity|NaN/)).toHaveCount(0);
});

test("insights: the explorer filters by age group, sex and area", async ({ page, context }) => {
  const seen: string[] = [];
  await bootB(page, context, "observer", {
    ...popHandlers(0),
    "GET /analytics/population-explorer": (_r: unknown, url: URL) => {
      seen.push(url.search);
      return { body: ok(explorerPayload(url)) };
    },
  });
  await page.goto("/insights");
  const card = page.getByRole("region", { name: "Population explorer" });
  await expect(card).toContainText("residents");
  await card.getByLabel("Age group").selectOption("50-64");
  await card.getByLabel("Sex").selectOption("female");
  await expect(card.getByRole("img", { name: /of 3,100 residents are due for care/ })).toBeVisible();
  expect(seen.some((s) => s.includes("age_band=50-64") && s.includes("sex=female"))).toBe(true);
  await card.getByLabel("Area").selectOption("D05");
  await expect(card.getByRole("img", { name: /of 310 residents are due for care/ })).toBeVisible();
  await card.getByRole("button", { name: "Clear filters" }).click();
  await expect(card.getByLabel("Age group")).toHaveValue("");
  // a pyramid band row filters the explorer too
  await page.getByRole("list", { name: "Population pyramid by age band and sex" }).getByRole("button", { name: "80 and over" }).click();
  await expect(card.getByLabel("Age group")).toHaveValue("80+");
  await expect(page.getByRole("heading", { name: "Key population insights" })).toBeVisible();
  await expect(page.getByText("District placement is not available")).toHaveCount(0);
  await expect(page.getByText(/residents are due for care/).first()).toBeVisible();
  await expectNoDashes(page);
  await page.mouse.move(1000, 150);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(600);
  await page.screenshot({ path: "test-results/screenshots/ref-insights.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-insights-full.png", fullPage: true });
});

test("reports: programme report, CSV download, save, recent and share", async ({ page, context }) => {
  await bootB(page, context, "observer", popHandlers(0));
  await page.goto("/reports");
  await expect(page.getByRole("heading", { level: 1, name: "Reports and Insights" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Executive summary" })).toBeVisible();
  await page.getByRole("combobox", { name: /^Report type/ }).selectOption("programme");
  await page.getByRole("combobox", { name: /^Programme/ }).selectOption("cervical_cancer_screening");
  await page.getByRole("button", { name: "Generate report" }).click();
  await expect(page.getByRole("heading", { level: 3, name: "Cervical cancer screening Programme Report" })).toBeVisible();
  const table = page.getByRole("table", { name: "Report: performance of each programme" });
  await expect(table).toContainText("Cervical cancer screening");
  await expect(table).not.toContainText("Breast cancer screening");
  await expect(table).toContainText("78%"); // 21,000 of 27,000
  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download CSV" }).click()]);
  expect(dl.suggestedFilename()).toBe("anticipa-programme-report.csv");
  const csv = await (await import("node:fs/promises")).readFile((await dl.path())!, "utf8");
  expect(csv).toContain("Cervical cancer screening");
  expect(csv).toContain("Screening coverage");
  await page.getByRole("button", { name: "Save report" }).click();
  await expect(page.getByRole("status")).toContainText("Saved on this device");
  await page.getByRole("tab", { name: "Saved reports" }).click();
  await expect(page.getByRole("list").filter({ hasText: "Programme performance" })).toContainText("Cervical cancer screening");
  await page.getByRole("tab", { name: "Recent reports" }).click();
  await expect(page.getByRole("list").filter({ hasText: "Programme performance" })).toContainText("Cervical cancer screening");
  await page.getByRole("button", { name: "Open" }).first().click();
  await expect(page.getByRole("heading", { level: 3, name: "Cervical cancer screening Programme Report" })).toBeVisible();
  await page.getByRole("button", { name: "Copy link" }).first().click();
  await expect(page.getByRole("status")).toBeVisible();
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.mouse.move(1000, 150);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(600);
  await page.screenshot({ path: "test-results/screenshots/ref-reports.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-reports-full.png", fullPage: true });
});

test("a shared link rebuilds the same report", async ({ page, context }) => {
  await bootB(page, context, "observer", popHandlers(0));
  await page.goto("/reports?type=district&district=D05&days=14&sections=equity");
  await expect(page.getByRole("heading", { level: 3, name: "District Comparison Report" })).toBeVisible();
  await expect(page.getByLabel("Time period")).toHaveValue("14");
  const table = page.getByRole("table", { name: "Report: coverage and closure by district" });
  await expect(table).toContainText("D05");
  await expect(table).not.toContainText("D01");
});

for (const path of ["/population", "/insights", "/reports"]) {
  for (const w of [1440, 390]) {
    test(`${path} at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
      await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
      await bootB(page, context, "observer", popHandlers(0));
      await page.goto(path);
      await expect(page.locator("#main h1")).toBeVisible();
      await page.waitForLoadState("networkidle");
      await expect(page.locator(".skeleton")).toHaveCount(0, { timeout: 15_000 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
      expect(fmtViolations((await axeScan(page)).violations)).toEqual([]);
    });
  }
}

test("a report with every section passes axe and does not scroll the page at 390px", async ({ page, context }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await bootB(page, context, "observer", popHandlers(0));
  await page.goto("/reports?type=population&sections=population,programmes,journey,trends,equity");
  await expect(page.getByRole("table", { name: "Report: coverage and closure by district" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Report: performance of each programme" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(fmtViolations((await axeScan(page)).violations)).toEqual([]);
});
