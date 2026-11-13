import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes, funnelPayload } from "./features-fixtures";
import { careGapRows, commandCenter, engagementOverview } from "./features-fixtures-ref";
import { careGapTotals, guidelinesList, programmesPayload } from "./features-fixtures-ref2";
import { ok, paged } from "./helpers";

// Population care journey page (reference: Anticipa Preventive Care Journey Dashboard), route /journey.
const handlers = {
  "GET /analytics/funnel": () => ({ body: ok(funnelPayload()) }),
  "GET /analytics/command-center": () => ({ body: ok(commandCenter()) }),
  "GET /analytics/engagement": () => ({ body: ok(engagementOverview()) }),
  "GET /guidelines": () => ({ body: paged(guidelinesList()) }),
  "GET /analytics/programmes": () => ({ body: ok(programmesPayload()) }),
  "GET /care-gaps": (_r: unknown, url: URL) => {
    const t = careGapTotals(url);
    return { body: paged(careGapRows(3), 1, Number(url.searchParams.get("page_size") ?? 15), t.total) };
  },
};

test("overview: five stage cards, funnel, cohort progress and insights from live figures", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/journey");
  await expect(page.getByRole("heading", { level: 1, name: "The preventive care journey" })).toBeVisible();
  const stages = page.getByRole("list", { name: "Journey stages" });
  await expect(stages.getByRole("listitem")).toHaveCount(5);
  await expect(stages.locator('[data-stage="identify"]')).toContainText("30,000");
  await expect(stages.locator('[data-stage="plan"]')).toContainText("12,000");
  await expect(stages.locator('[data-stage="plan"]')).toContainText("40%"); // 12,000 of 30,000
  await expect(stages.locator('[data-stage="attend"]')).toContainText("1,500"); // appointments attended
  await expect(stages.locator('[data-stage="complete"]')).toContainText("900");
  const funnel = page.getByRole("list", { name: "Journey funnel" });
  await expect(funnel).toContainText("100%");
  const cohorts = page.getByRole("region", { name: "Cohort progress table" });
  await expect(cohorts).toContainText("High priority");
  await expect(cohorts).toContainText("4,400");
  await page.getByRole("button", { name: "By district" }).click();
  await expect(cohorts).toContainText("District MUM");
  await expect(page.getByText("Capacity nearly full")).toBeVisible();
  await expect(page.getByText(/main drop off/)).toBeVisible();
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.screenshot({ path: "test-results/screenshots/ref-journey.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-journey-full.png", fullPage: true });
});

test("tabs: cohorts, bottlenecks and case list open real data and link to resident journeys", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/journey");
  await page.getByRole("tab", { name: "Bottlenecks" }).click();
  await expect(page.getByRole("table", { name: "Stage to stage conversion" })).toContainText("Lost");
  await expect(page.getByText("Escalated")).toBeVisible();
  await page.getByRole("tab", { name: "Case list" }).click();
  const table = page.getByRole("table", { name: "Care gap cases" });
  await expect(table.getByRole("row")).toHaveCount(4);
  await expect(table.getByRole("link", { name: "Open journey" }).first()).toHaveAttribute("href", /\/residents\/cccccccc-.*\/journey/);
  await page.getByRole("tab", { name: "Cohorts" }).click();
  await expect(page.getByRole("region", { name: "Cohort progress table" })).toBeVisible();
  // Keyboard: arrow keys move between tabs.
  await page.getByRole("tab", { name: "Cohorts" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Bottlenecks" })).toHaveAttribute("aria-selected", "true");
});

for (const w of [1440, 390]) {
  test(`journey at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await bootB(page, context, "observer", handlers);
    await page.goto("/journey");
    await expect(page.getByRole("list", { name: "Journey funnel" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}

test("filters re-count the stages and every stage opens the real cases in a drawer", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/journey");
  const stages = page.getByRole("list", { name: "Journey stages" });
  await expect(stages.locator('[data-stage="identify"]')).toContainText("30,000");
  // A programme filter switches the stages to cases for that programme.
  await page.getByLabel("Programme").selectOption("breast_cancer_screening");
  await expect(stages.locator('[data-stage="identify"]')).toContainText("13,000");
  await expect(page.getByText("Counts are cases for this filter.")).toBeVisible();
  await page.locator("#jf-dist").selectOption("PUN");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(stages.locator('[data-stage="identify"]')).toContainText("30,000");
  // Drill down: the Plan stage opens a dialog with real cases that link to resident journeys.
  const plan = page.getByRole("button", { name: /^Plan 12,000/ });
  await plan.click();
  const dialog = page.getByRole("dialog", { name: /Plan: care plans created/ });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("list", { name: "Matching cases" }).getByRole("listitem")).toHaveCount(3);
  await expect(dialog.getByRole("link", { name: /Open the care gap list/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(plan).toBeFocused(); // focus returns to the control that opened it
  // A funnel bar and a cohort row open the same drawer with their own filter.
  await page.getByRole("button", { name: /^Engage 20%/ }).click();
  await expect(page.getByRole("dialog", { name: /Engage/ })).toBeVisible();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: /High priority/ }).first().click();
  await expect(page.getByRole("dialog", { name: "High priority" })).toBeVisible();
});

test("the daily activity chart shows opened and closed cases with a table view", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/journey");
  await expect(page.getByRole("region", { name: "Daily activity" })).toContainText("Cases opened and cases closed per day");
  await page.getByRole("region", { name: "Daily activity" }).getByText("View as table").click();
  await expect(page.getByRole("table", { name: "Cases opened and closed per day" })).toBeVisible();
});
