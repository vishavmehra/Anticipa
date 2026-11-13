import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes, funnelPayload } from "./features-fixtures";
import { evalRun, evalRunsList } from "./features-fixtures-eval";
import { commandCenter } from "./features-fixtures-ref";
import { capacityOverview, careGapTotals, geographyCountry, geographyState, guidelinesList, programmesPayload } from "./features-fixtures-ref2";
import { ok, paged } from "./helpers";

// Programs and impact (reference: Anticipa Programs Impact Dashboard), route /evaluation.
const seen: string[] = [];
const handlers = {
  "GET /guidelines": () => ({ body: paged(guidelinesList()) }),
  "GET /analytics/programmes": () => ({ body: ok(programmesPayload()) }),
  "GET /care-gaps": (_r: unknown, url: URL) => {
    seen.push(url.search);
    const t = careGapTotals(url);
    return { body: paged(t.rows, 1, 1, t.total) };
  },
  "GET /analytics/command-center": () => ({ body: ok(commandCenter()) }),
  "GET /analytics/geography": (_r: unknown, url: URL) => ({ body: ok(url.searchParams.get("level") === "state" ? geographyState(url.searchParams.get("code") ?? "27") : geographyCountry()) }),
  "GET /analytics/capacity": () => ({ body: ok(capacityOverview()) }),
  "GET /analytics/funnel": () => ({ body: ok(funnelPayload()) }),
  "GET /analytics/evaluation": () => ({ body: ok(evalRun()) }),
  "GET /analytics/evaluation/runs": () => ({ body: paged(evalRunsList()) }),
};

test("impact: programme chips, live stage counts, gauge, equity and an arithmetic projection", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/evaluation");
  await expect(page.getByRole("heading", { level: 1, name: "Programs and impact" })).toBeVisible();
  const chips = page.getByRole("group", { name: "Programme" });
  await expect(chips.getByRole("button")).toHaveCount(3); // all programmes plus two approved activities
  const kpis = page.getByRole("region", { name: "Programme figures" });
  await expect(kpis).toContainText("26,000");
  await expect(kpis).toContainText("1,500");
  await expect(kpis).toContainText("900");
  await expect(kpis).toContainText("48%"); // 12,500 of 26,000 planned
  const flow = page.getByRole("list", { name: "Care journey stages" });
  await expect(flow.locator('[data-stage="contacted"]')).toContainText("6,100");
  await expect(page.getByRole("img", { name: /Closure rate: 3%/ })).toBeVisible();
  await expect(page.getByText("6 points")).toBeVisible();
  await expect(page.getByText("Not available").first()).toBeVisible();
  await expect(page.getByRole("list", { name: "States" })).toContainText("Maharashtra");
  await expect(page.getByRole("list", { name: "States" })).not.toContainText("Goa"); // no residents there
  await expect(page.getByRole("list", { name: "Closure rate by cohort" })).toContainText("High priority");
  await expect(page.getByText("+3,100")).toBeVisible();
  await expect(page.getByText("Short by 1,100 seats")).toBeVisible();
  await page.getByLabel("Target planned coverage").fill("50");
  await expect(page.getByText("+500")).toBeVisible();
  await expect(page.getByText("The open seats would cover the extra cases.")).toBeVisible();
  await chips.getByRole("button", { name: "Breast cancer screening" }).click();
  await expect(kpis).toContainText("13,000");
  expect(seen.some((s) => s.includes("care_activity=breast_cancer_screening"))).toBe(true);
  await expect(page.getByRole("list", { name: "Care gap funnel stages" })).toBeVisible();
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.screenshot({ path: "test-results/screenshots/ref-impact.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-impact-full.png", fullPage: true });
});

test("the measured quality tab holds the evaluation dashboard", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/evaluation");
  await page.getByRole("tab", { name: "Measured quality" }).click();
  await expect(page.getByRole("table", { name: "Quality checks" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Headline results" })).toContainText("99.1%");
});

for (const w of [1440, 390]) {
  test(`impact at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await bootB(page, context, "observer", handlers);
    await page.goto("/evaluation");
    await expect(page.getByRole("list", { name: "Care journey stages" })).toBeVisible();
    await expect(page.getByRole("list", { name: "States" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}

test("district ranking cross filters the figures and a stage opens its cases", async ({ page, context }) => {
  seen.length = 0;
  await bootB(page, context, "observer", handlers);
  await page.goto("/evaluation");
  await page.getByRole("list", { name: "States" }).getByRole("button", { name: /Maharashtra/ }).click();
  const ranking = page.getByRole("list", { name: "Districts" });
  await expect(ranking).toContainText("Pune");
  await ranking.getByRole("button", { name: /PUN/ }).click();
  await expect(page.getByRole("button", { name: /District PUN is selected/ })).toBeVisible();
  await expect.poll(() => seen.some((s) => s.includes("district=PUN"))).toBe(true);
  await page.getByRole("button", { name: /District PUN is selected/ }).click();
  await expect(page.getByRole("button", { name: /District PUN is selected/ })).toHaveCount(0);
  await page.getByRole("button", { name: /^Contacted 6,100/ }).click();
  const dialog = page.getByRole("dialog", { name: /Contacted cases/ });
  await expect(dialog.getByRole("list", { name: "Matching cases" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("the equity gap has a hover definition", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/evaluation");
  await page.getByRole("button", { name: "What this means" }).first().hover();
  await expect(page.locator('[role="tooltip"]:not(#ask-tip)')).toContainText("Highest minus lowest district planned coverage");
});
