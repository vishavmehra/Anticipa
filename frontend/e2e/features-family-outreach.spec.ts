import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { DOCS } from "./fixtures";
import { R_ID, bootB, expectNoDashes, journeyHandlers, journeyPayload, pedigreePayload, previewPayload } from "./features-fixtures";

// Family pedigree (feature 9) and multilingual outreach preview (feature 11), both shown on the journey page.
const WIDTHS = [
  { w: 1440, h: 900 },
  { w: 390, h: 844 },
] as const;

test("pedigree draws stored relatives and lists each relative's cancer history with its source", async ({ page, context }) => {
  await bootB(page, context, "observer", journeyHandlers());
  await page.goto(`/residents/${R_ID}/journey`);
  const card = page.getByRole("region", { name: "Family history" });
  await expect(card).toContainText("4 stored relatives, 3 of them residents, 2 with cancer history on file");
  const svg = card.getByRole("group", { name: /Pedigree of the resident with 4 stored relatives/ });
  await expect(svg).toBeVisible();
  // Three linked relatives are links to their own records; the resident and the unlinked father are not.
  await expect(svg.getByRole("link")).toHaveCount(3);
  await expect(svg.locator('circle[fill="url(#affected)"]')).toHaveCount(2); // mother and maternal aunt: cancer history on file
  await expect(svg.locator("text", { hasText: "This resident" })).toBeVisible();
  await expect(svg.locator("text", { hasText: "Maternal aunt" })).toBeVisible();
  const list = card.getByRole("list", { name: "Relatives" });
  await expect(list.getByRole("listitem")).toHaveCount(4);
  const mother = list.locator('[data-relative="mother"]');
  await expect(mother).toContainText("First degree relative");
  await expect(mother).toContainText("From a clinical record");
  await expect(mother).toContainText("Malignant neoplasm of breast");
  await expect(mother).toContainText("Stated in this resident's note");
  await expect(mother.getByText(DOCS.en.quote)).toBeVisible();
  await expect(list.locator('[data-relative="maternal_aunt"]')).toContainText("From the relative's own note");
  await expect(list.locator('[data-relative="father"]')).toContainText("Not a resident");
  await expect(list.locator('[data-relative="father"]')).toContainText("No cancer history on file for this relative");
  await expect(list.locator('[data-relative="sister"]')).toContainText("Your role cannot view clinical records for this relative");
  // The quote opens in its note.
  await mother.getByRole("button", { name: "Show quote in note" }).click();
  await expect(mother.getByTestId("note-viewer").locator("mark")).toHaveText(DOCS.en.quote);
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expectNoDashes(page);
});

test("pedigree says so when the resident has no stored relatives", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...journeyHandlers(), [`GET /residents/${R_ID}/pedigree`]: () => ({ body: { data: { ...pedigreePayload(), relatives: [], counts: { relatives: 0, linked_relatives: 0, relatives_with_cancer_history: 0 } }, meta: { request_id: "r" } } }) });
  await page.goto(`/residents/${R_ID}/journey`);
  await expect(page.getByText("No relatives are stored for this resident.")).toBeVisible();
});

test("outreach preview shows the reminder in English, Hindi and Arabic, Arabic right to left", async ({ page, context }) => {
  await bootB(page, context, "observer", journeyHandlers());
  await page.goto(`/residents/${R_ID}/journey`);
  const card = page.getByRole("region", { name: "Reminder in three languages" });
  const items = card.getByRole("list", { name: "Reminder in each language" }).getByRole("listitem");
  await expect(items).toHaveCount(3);
  const ar = card.locator('[data-language="ar"]');
  await expect(ar.locator("p[lang=ar]").first()).toHaveAttribute("dir", "rtl");
  await expect(ar.locator("p[lang=ar]").first()).toContainText(previewPayload().previews[2].body);
  await expect(ar).toContainText("Resident's language");
  await expect(ar).toContainText("Reads right to left");
  await expect(card.locator('[data-language="hi"] p[lang=hi]').first()).toContainText("अनुस्मारक");
  await expect(card.locator('[data-language="en"]')).toContainText("Reads left to right");
  await expect(card.getByText("AI calls made: 0")).toBeVisible();
  await expect(card.getByText("Simulated").first()).toBeVisible();
  // Delivery timeline from the real recorded message: created, sent, delivered, replied.
  const timeline = card.getByRole("list", { name: "Simulated delivery timeline" });
  await expect(timeline).toContainText("Simulated delivery");
  const steps = timeline.getByRole("list", { name: /Delivery steps/ }).getByRole("listitem");
  await expect(steps).toHaveCount(4);
  await expect(steps.nth(2)).toContainText("Delivered");
  await expect(steps.nth(2)).toContainText("12 Nov 2026, 08:00 UTC");
  await expect(steps.nth(3)).toContainText("Resident replied");
  await expect(timeline).toContainText("Reply recorded: Accepted");
  await expectNoDashes(page);
});

test("outreach timeline shows a failed delivery as not happened, and an empty case as not yet sent", async ({ page, context }) => {
  const payload = journeyPayload();
  const msg = payload.cases.items[0].communications![0];
  Object.assign(msg, { delivery_state: "failed", delivered_at: null, response_received_at: null, responses: [] });
  await bootB(page, context, "observer", journeyHandlers(payload));
  await page.goto(`/residents/${R_ID}/journey`);
  const timeline = page.getByRole("list", { name: "Simulated delivery timeline" });
  await expect(timeline.getByText("Did not happen")).toHaveCount(2);
});

for (const { w, h } of WIDTHS) {
  test(`pedigree and outreach at ${w}px: screenshot, axe, no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: h });
    await bootB(page, context, "observer", journeyHandlers());
    await page.goto(`/residents/${R_ID}/journey`);
    await expect(page.getByRole("group", { name: /Pedigree of the resident/ })).toBeVisible();
    await expect(page.getByRole("list", { name: "Reminder in each language" })).toBeVisible();
    await page.screenshot({ path: `test-results/screenshots/features-pedigree-outreach-${w}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}
