import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { capacityOverview, careGapTotals, guidelinesList } from "./features-fixtures-ref2";
import { ok, paged } from "./helpers";

// Care planning studio (reference: Anticipa Care Planning Studio), route /planning.
const run = {
  id: "ffffffff-0000-4000-8000-0000000000b1",
  status: "completed",
  planning_period_start: "2026-11-12T00:00:00Z",
  planning_period_end: "2026-11-19T00:00:00Z",
  configuration_version: "v1",
  algorithm_version: "a1",
  input_snapshot_hash: null,
  seed: 42,
  config: {},
  solver_parameters: {},
  case_filters: {},
  metrics: { cases_total: 9000, allocated: 7000, unallocated: 2000, coverage: 0.78 },
  error_summary: null,
  job_id: null,
  created_by: null,
  created_at: "2026-11-12T07:00:00Z",
  started_at: "2026-11-12T07:00:01Z",
  completed_at: "2026-11-12T07:00:20Z",
  synthetic_status: "simulated",
};
const posted: unknown[] = [];
const handlers = () => ({
  "GET /care-gaps": (_r: unknown, url: URL) => ({ body: paged(careGapTotals(url).rows, 1, 1, url.searchParams.getAll("status").includes("approved_for_planning") ? 9000 : 26000) }),
  "GET /guidelines": () => ({ body: paged(guidelinesList()) }),
  "GET /analytics/capacity": () => ({ body: ok(capacityOverview()) }),
  "GET /planning/runs": () => ({ body: paged([run], 1, 1, 1) }),
  "GET /planning/clinics": () => ({ body: paged([], 1, 25, 0) }),
  "POST /planning/runs": (req: { postDataJSON: () => unknown }) => {
    posted.push(req.postDataJSON());
    return { status: 202, body: ok({ job_id: "aaaaaaaa-0000-4000-8000-0000000000a9", created: true, status: "queued" }) };
  },
});

test("studio: steps from real state, summary from live counts, scenarios and an admin generates a plan", async ({ page, context }) => {
  await bootB(page, context, "population_admin", handlers());
  await page.goto("/planning");
  await expect(page.getByRole("heading", { level: 1, name: "Care planning studio" })).toBeVisible();
  const steps = page.getByRole("list", { name: "Planning steps" });
  await expect(steps.locator('[data-step="1"]')).toHaveAttribute("data-state", "done");
  await expect(steps.locator('[data-step="1"]')).toContainText("9,000 cases waiting for a slot");
  await expect(steps.locator('[data-step="2"]')).toContainText("2 screenings with an approved guideline");
  await expect(steps.locator('[data-step="3"]')).toContainText("2,000 seats open this week");
  await expect(steps.locator('[data-step="4"]')).toHaveAttribute("data-state", "current");
  const summary = page.getByRole("region", { name: "Plan summary" });
  await expect(summary).toContainText("9,000");
  await expect(summary).toContainText("Short by 7,000 seats");
  await expect(summary).toContainText("7,000 of 9,000 cases allocated");
  await expect(page.getByRole("radiogroup", { name: "Planning scenario" }).getByRole("radio")).toHaveCount(3);
  await page.getByRole("radio", { name: /High risk first/ }).click();
  await page.getByRole("button", { name: "Generate plan" }).click();
  await expect(page.getByText(/The plan was submitted for the next 7 days/)).toBeVisible();
  const body = posted[0] as { planning_period: { start: string; end: string }; priority_weights: Record<string, number> };
  expect(body.priority_weights.high).toBe(12);
  expect(new Date(body.planning_period.end).getTime() - new Date(body.planning_period.start).getTime()).toBe(7 * 24 * 3600 * 1000);
  await expect(page.getByText(/synthetic|draft/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.screenshot({ path: "test-results/screenshots/ref-planning.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-planning-full.png", fullPage: true });
});

test("an observer sees the studio and a disabled Generate plan with the reason", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/planning");
  const btn = page.getByRole("button", { name: /Generate plan/ });
  await expect(btn).toHaveAttribute("aria-disabled", "true");
  await expect(btn).toHaveAttribute("title", "Read only access");
  const before = posted.length;
  await btn.click({ force: true });
  expect(posted.length).toBe(before); // a read only click submits nothing
});

test("the other tabs keep the capacity, allocation and run views", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/planning");
  await page.getByRole("tab", { name: "Capacity" }).click();
  await expect(page.getByRole("heading", { name: "Clinics", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Track" }).click();
  await expect(page.getByRole("table", { name: "Planning runs" })).toBeVisible();
});

for (const w of [1440, 390]) {
  test(`planning at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await bootB(page, context, "observer", handlers());
    await page.goto("/planning");
    await expect(page.getByRole("region", { name: "Plan summary" })).toContainText("9,000");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}

test("latest run shows what changed since the run before and why cases were left out", async ({ page, context }) => {
  const newer = { ...run, id: "ffffffff-0000-4000-8000-0000000000b2", created_at: "2026-11-12T09:00:00Z", metrics: { cases_total: 9500, allocated: 7600, unallocated: 1900, coverage: 0.8, reason_counts: { no_capacity: 1500, language_unsupported: 400 } } };
  await bootB(page, context, "observer", { ...handlers(), "GET /planning/runs": () => ({ body: paged([newer, run], 1, 2, 2) }) });
  await page.goto("/planning");
  const latest = page.getByRole("region", { name: "Latest run" });
  await expect(latest).toContainText("+500 since the run before"); // cases in the run
  await expect(latest).toContainText("+600 since the run before"); // allocated
  await expect(latest).toContainText("+2.0 points since the run before"); // coverage
  const reasons = latest.getByRole("list", { name: "Reasons cases were left out" });
  await expect(reasons).toContainText("No capacity");
  await expect(reasons).toContainText("1,500");
  await latest.getByRole("button", { name: /See the allocations/ }).click();
  await expect(page.getByRole("tab", { name: "Allocate" })).toHaveAttribute("aria-selected", "true");
});
