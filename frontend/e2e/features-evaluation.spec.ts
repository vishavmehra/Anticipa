import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes, funnelPayload } from "./features-fixtures";
import { evalRun, evalRunsList } from "./features-fixtures-eval";
import { commandCenter } from "./features-fixtures-ref";
import { capacityOverview, careGapTotals, guidelinesList, programmesPayload } from "./features-fixtures-ref2";
import { ok, paged } from "./helpers";

// Evaluation dashboard (feature 6), route /evaluation.
const WIDTHS = [
  { w: 1440, h: 900 },
  { w: 390, h: 844 },
] as const;
const handlers = (run: object = evalRun()) => ({
  "GET /guidelines": () => ({ body: paged(guidelinesList()) }),
  "GET /analytics/programmes": () => ({ body: ok(programmesPayload()) }),
  "GET /care-gaps": (_r: unknown, url: URL) => ({ body: paged(careGapTotals(url).rows, 1, 1, careGapTotals(url).total) }),
  "GET /analytics/command-center": () => ({ body: ok(commandCenter()) }),
  "GET /analytics/capacity": () => ({ body: ok(capacityOverview()) }),
  "GET /analytics/funnel": () => ({ body: ok(funnelPayload()) }),
  "GET /analytics/evaluation": () => ({ body: ok(run) }),
  "GET /analytics/evaluation/runs": () => ({ body: paged(evalRunsList()) }),
  "GET /analytics/evaluation/runs/20261111T080000-ee12cd34": () => ({ body: ok(evalRun({ run_id: "20261111T080000-ee12cd34", run_type: "smoke" })) }),
});

test("shows measured results by language and category, checks, cost and latency from the run", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/evaluation");
  await page.getByRole("tab", { name: "Measured quality" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Programs and impact" })).toBeVisible();
  const tiles = page.getByRole("region", { name: "Headline results" });
  await expect(tiles).toContainText("5 of 7");
  await expect(tiles).toContainText("99.1%");
  await expect(tiles).toContainText("1.2%");
  await expect(tiles).toContainText("$4.20");
  await expect(tiles).toContainText("812 ms");
  await expect(tiles).toContainText("Estimated from configured prices");
  const byLang = page.getByRole("list", { name: "Precision by language" });
  await expect(byLang).toContainText("English");
  await expect(byLang).toContainText("97.0%");
  await expect(page.getByText("Few samples, read with care: Arabic.").first()).toBeVisible();
  await page.getByLabel("Metric").first().selectOption({ label: "Recall" });
  await expect(page.getByText("Not measured: Arabic.")).toBeVisible();
  await expect(page.getByRole("table", { name: "Metrics by language" })).toContainText("Hindi");
  await expect(page.getByRole("list", { name: "Precision by category" })).toContainText("Family history");
  await expect(page.getByRole("table", { name: "Safety checks" })).toContainText("accepted facts lacking a verified quote");
  const gates = page.getByRole("table", { name: "Quality checks" });
  await expect(gates.getByRole("row")).toHaveCount(5);
  await page.getByLabel("Show", { exact: true }).selectOption("fail");
  await expect(gates.getByRole("row")).toHaveCount(2);
  await expect(gates).toContainText("SG-08a on EX-12");
  await expect(gates).toContainText("(proposed)");
  await expect(page.getByRole("table", { name: "Cost by model and task" })).toContainText("anthropic/test-model/evidence.extract");
  await expect(page.getByRole("list", { name: "Coverage against target" })).toContainText("2,400 of 10,000");
  await expect(page.getByText("Generated text is not clinical text")).toBeVisible();
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.getByLabel("Run", { exact: true }).selectOption({ index: 1 });
  await expect(page.getByText("20261111T080000-ee12cd34").first()).toBeVisible();
});

test("a metric that was not measured says so and is never shown as zero", async ({ page, context }) => {
  const run = evalRun();
  run.metrics["EX-06"] = { value: null, numerator: 0, denominator: 0, n: 0, unit: "ratio", ci95: null, status: "not_applicable", flags: [] };
  run.metrics["PF-01"] = { value: null, status: "not_applicable" };
  await bootB(page, context, "observer", handlers(run));
  await page.goto("/evaluation");
  await page.getByRole("tab", { name: "Measured quality" }).click();
  const tiles = page.getByRole("region", { name: "Headline results" });
  await expect(tiles.getByText("Not available")).toHaveCount(2);
  await expect(tiles).not.toContainText("0.0%");
});

test("honest empty state when no run exists yet", async ({ page, context }) => {
  await bootB(page, context, "observer", {
    "GET /analytics/evaluation": () => ({ status: 404, body: { error: { code: "NOT_FOUND", message: "No evaluation run exists yet", details: [], request_id: "r" } } }),
    "GET /analytics/evaluation/runs": () => ({ body: paged([]) }),
    "GET /analytics/funnel": () => ({ body: ok(funnelPayload()) }),
  });
  await page.goto("/evaluation");
  await page.getByRole("tab", { name: "Measured quality" }).click();
  await expect(page.getByText("No evaluation run yet")).toBeVisible();
  await expect(page.getByRole("region", { name: "Headline results" })).toHaveCount(0);
});

for (const { w, h } of WIDTHS) {
  test(`evaluation at ${w}px: screenshot, axe, no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: h });
    await bootB(page, context, "observer", handlers());
    await page.goto("/evaluation");
  await page.getByRole("tab", { name: "Measured quality" }).click();
    await expect(page.getByRole("table", { name: "Quality checks" })).toBeVisible();
    await page.screenshot({ path: `test-results/screenshots/features-evaluation-${w}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}
