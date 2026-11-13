import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { DOCS } from "./fixtures";
import { R_ID, bootB, expectNoDashes, journeyHandlers, journeyPayload } from "./features-fixtures";

// Journey stepper, "Why is this person due?" explainer and the evidence-in-note viewer (features 1 to 3).
const WIDTHS = [
  { w: 1440, h: 900 },
  { w: 390, h: 844 },
] as const;

const open = async (page: Page, payload = journeyPayload()) => {
  await page.goto(`/residents/${R_ID}/journey`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Journey of resident cccccccc");
  return payload;
};

test.describe("journey", () => {
  test("stepper shows five stages from the real records and opens the current stage", async ({ page, context }) => {
    await bootB(page, context, "population_admin", journeyHandlers());
    await open(page);
    const stepper = page.getByRole("navigation", { name: "Resident journey stages" });
    await expect(stepper.getByRole("button")).toHaveCount(5);
    for (const [stage, state] of [["read", "complete"], ["decide", "complete"], ["plan", "complete"], ["act", "current"], ["close", "later"]] as const) {
      await expect(stepper.locator(`[data-stage="${stage}"]`)).toHaveAttribute("data-state", state);
    }
    await expect(stepper.locator('[data-stage="act"]')).toHaveAttribute("aria-current", "step");
    // ACT: the simulated Arabic reminder renders right to left, labelled as a simulated message.
    const msg = page.getByRole("list", { name: "Simulated messages" }).getByRole("listitem").first();
    await expect(msg).toContainText("Simulated message");
    await expect(msg.locator("p[lang=ar]")).toHaveAttribute("dir", "rtl");
    await expect(page.getByText("Resident response (simulated): Accepted")).toBeVisible();
    await expectNoDashes(page);
  });

  test("decide stage explains the decision criterion by criterion", async ({ page, context }) => {
    await bootB(page, context, "observer", journeyHandlers());
    await open(page);
    await page.getByRole("button", { name: /^Decide/ }).click();
    const why = page.getByTestId("why-due");
    await expect(why).toBeVisible();
    await expect(why.locator('[data-criterion-state="pass"]')).toHaveCount(3);
    await expect(why.locator('[data-criterion-state="fail"]')).toHaveCount(1);
    await expect(why.locator('[data-criterion-state="missing"]')).toHaveCount(1);
    await expect(why.locator('[data-criterion-state="conflicting"]')).toHaveCount(1);
    await expect(why.getByText("Rule status: Approved")).toBeVisible();
    await expect(why.getByText(/pending clinical review/i)).toHaveCount(0);
    await expect(why.getByText("breast_cancer_screening").first()).toBeVisible();
    await expect(why).toContainText("version 2");
    await expect(why).toContainText("family_history_pathway");
    await expect(why).toContainText("Age 50 to 74");
    await expect(why).toContainText("No mammogram in 24 months");
    await expect(why).toContainText("Example Task Force, 2024");
    await expect(why).toContainText("30 Apr 2024");
    await expect(why).toContainText("B");
    await expect(why).toContainText("Source status: Final");
    await expect(why).toContainText("It does not decide who is due");
    await expectNoDashes(page);
    // The other evaluation is for another guideline: switching shows its own explanation.
    await page.getByRole("button", { name: "Why?" }).click();
    await expect(page.getByTestId("why-due")).toContainText("cervical_cancer_screening");
  });

  test("evidence link opens the note with the quote highlighted at its offsets", async ({ page, context }) => {
    await bootB(page, context, "population_admin", journeyHandlers());
    await open(page);
    await page.getByRole("button", { name: /^Decide/ }).click();
    await page.getByTestId("why-due").getByRole("button", { name: "Show quote in note" }).click();
    const viewer = page.getByTestId("why-due").getByTestId("note-viewer");
    await expect(viewer.locator("mark")).toHaveText(DOCS.en.quote);
    await expect(viewer.getByText("matches the stored quote exactly")).toBeVisible();
    await expect(viewer.getByText("Stated")).toBeVisible();
    await expect(viewer.getByText("Relative: Mother")).toBeVisible();
    await expect(viewer.getByText("Timing: Historical")).toBeVisible();
    await expect(viewer.getByText("Quote verified (exact)")).toBeVisible();
    await expect(viewer.getByText("evidence.extract.v3")).toBeVisible();
    await expect(viewer.getByText("test-model")).toBeVisible();
  });

  test("read stage: Arabic note is right to left and Devanagari stays intact", async ({ page, context }) => {
    await bootB(page, context, "population_admin", journeyHandlers());
    await open(page);
    await page.getByRole("button", { name: /^Read/ }).click();
    const items = page.getByRole("region", { name: /^Read:/ }).getByRole("list").first().getByRole("listitem");
    await expect(items).toHaveCount(3);
    await items.nth(1).getByRole("button", { name: "Show quote in note" }).click(); // Arabic fact
    const ar = page.getByTestId("note-viewer");
    await expect(ar.getByRole("region", { name: /Source note/ })).toHaveAttribute("dir", "rtl");
    await expect(ar.locator("mark")).toHaveText(DOCS.ar.quote);
    await items.nth(2).getByRole("button", { name: "Show quote in note" }).click(); // Hindi fact
    await expect(page.getByTestId("note-viewer").nth(1).locator("mark")).toHaveText(DOCS.hi.quote);
    await expect(page.getByTestId("note-viewer").nth(1).getByText("Negated")).toBeVisible();
  });

  test("a role without evidence access sees that stage as restricted, the rest still works", async ({ page, context }) => {
    await bootB(page, context, "care_coordinator", journeyHandlers(journeyPayload({ evidence: null, restricted: ["evidence"] })));
    await open(page);
    await expect(page.locator('[data-stage="read"]')).toHaveAttribute("data-state", "unavailable");
    await page.getByRole("button", { name: /^Read/ }).click();
    await expect(page.getByText(/Your role cannot view this section: evidence extracted from notes/)).toBeVisible();
    await page.getByRole("button", { name: /^Decide/ }).click();
    await expect(page.getByTestId("why-due")).toBeVisible();
  });

  test("a resident with no case shows read and decide and says plan, act, close have not started", async ({ page, context }) => {
    await bootB(page, context, "observer", journeyHandlers(journeyPayload({ cases: { definition: "x", items: [] } })));
    await open(page);
    await expect(page.getByText("No care gap has been raised for this resident")).toBeVisible();
    await expect(page.locator('[data-stage="plan"]')).toHaveAttribute("data-state", "later");
  });
});

for (const { w, h } of WIDTHS) {
  test.describe(`journey at ${w}px`, () => {
    test.use({ viewport: { width: w, height: h } });
    for (const stage of ["Read", "Decide", "Plan", "Act", "Close"] as const) {
      test(`${stage}: screenshot, axe, no horizontal scroll`, async ({ page, context }) => {
        await bootB(page, context, "observer", journeyHandlers());
        await open(page);
        await page.getByRole("navigation", { name: "Resident journey stages" }).locator(`[data-stage="${stage.toLowerCase()}"]`).click();
        if (stage === "Decide") await page.getByTestId("why-due").getByRole("button", { name: "Show quote in note" }).click();
        if (stage === "Read") await page.getByRole("region", { name: /^Read:/ }).getByRole("button", { name: "Show quote in note" }).first().click();
        await expect(page.locator(".skeleton")).toHaveCount(0);
        if (stage === "Decide" || stage === "Read") await expect(page.getByTestId("note-viewer").locator("mark")).toBeVisible();
        await page.screenshot({ path: `test-results/screenshots/features-journey-${stage.toLowerCase()}-${w}.png`, fullPage: true });
        expect(await page.evaluate(() => document.documentElement.scrollWidth), "page must not scroll horizontally").toBeLessThanOrEqual(w);
        await expectNoDashes(page);
        const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
        expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
      });
    }
  });
}
