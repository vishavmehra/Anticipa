import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { careGapRows } from "./features-fixtures-ref";
import { guidelinesList, programmesPayload } from "./features-fixtures-ref2";
import { ok, paged } from "./helpers";

// Preventive care programs (reference: Preventive Care Programs Dashboard), route /programs.
const seen: string[] = [];
function totalFor(url: URL): number {
  const st = url.searchParams.getAll("status");
  const activity = url.searchParams.get("care_activity");
  const scale = activity === "cervical_cancer_screening" ? 0.5 : 1;
  const n = url.searchParams.get("priority") === "high" ? 400 : st.length === 0 ? 13000 : st.length > 5 ? 7000 : st[0] === "completed" ? 450 : st[0] === "outreach_completed" ? 120 : st[0] === "awaiting_completion_evidence" ? 80 : 0;
  return Math.round(n * scale);
}
const handlers = {
  "GET /guidelines": () => ({ body: paged(guidelinesList()) }),
  "GET /analytics/programmes": () => ({ body: ok(programmesPayload()) }),
  "GET /care-gaps": (_r: unknown, url: URL) => {
    seen.push(url.search);
    return { body: paged(careGapRows(1), 1, 1, totalFor(url)) };
  },
};

test("programs: active program with live counts, next best actions and every program", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/programs");
  await expect(page.getByRole("heading", { level: 1, name: "Preventive care programs" })).toBeVisible();
  // Breast cancer has the most cases (13,000), so it is the active program.
  await expect(page.getByRole("heading", { level: 2, name: "Breast cancer screening" })).toBeVisible();
  const figures = page.getByRole("region", { name: "Program figures" });
  await expect(figures).toContainText("13,000");
  await expect(figures).toContainText("7,000");
  await expect(figures).toContainText("450");
  const actions = page.getByRole("list", { name: "Next best actions" });
  await expect(actions).toContainText("400 open cases");
  await expect(actions).toContainText("120 residents contacted, no appointment yet");
  await expect(actions).toContainText("80 cases waiting for evidence");
  await expect(page.getByText("400 high priority")).toBeVisible();
    const all = page.getByRole("list", { name: "Programs" });
  await expect(all.getByRole("listitem")).toHaveCount(2);
  await expect(all.locator('[data-program="cervical_cancer_screening"]')).toContainText("6,500 cases, 225 completed");
  // Choosing another program recounts for it.
  await all.locator('[data-program="cervical_cancer_screening"] button').click();
  await expect(page.getByRole("heading", { level: 2, name: "Cervical cancer screening" })).toBeVisible();
  expect(seen.some((s) => s.includes("care_activity=cervical_cancer_screening"))).toBe(true);
  await expect(page.getByText(/synthetic|draft/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.screenshot({ path: "test-results/screenshots/ref-programs.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-programs-full.png", fullPage: true });
});

test("no cases yet: an honest empty state, no invented programs", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...handlers, "GET /analytics/programmes": () => ({ body: ok({ generated_at: "2026-11-12T08:00:00Z", population: 0, programmes: [] }) }) });
  await page.goto("/programs");
  await expect(page.getByText("No program has any cases yet")).toBeVisible();
});

for (const w of [1440, 390]) {
  test(`programs at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await bootB(page, context, "observer", handlers);
    await page.goto("/programs");
    await expect(page.getByRole("list", { name: "Programs" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}

test("every figure and action opens the cases behind it", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers);
  await page.goto("/programs");
  await page.getByRole("button", { name: "Open the cases: care completed" }).click();
  const dialog = page.getByRole("dialog", { name: /Care completed, Breast cancer screening/ });
  await expect(dialog.getByRole("list", { name: "Matching cases" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Open the cases: Review high priority cases" }).click();
  await expect(page.getByRole("dialog", { name: /Review high priority cases/ })).toBeVisible();
  expect(seen.some((s) => s.includes("priority=high") && s.includes("care_activity=breast_cancer_screening"))).toBe(true);
});
