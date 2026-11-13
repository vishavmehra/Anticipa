import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { DOCS } from "./fixtures";
import { bootB, expectNoDashes, factFor, journeyHandlers, journeyPayload } from "./features-fixtures";
import { guidelinesList } from "./features-fixtures-ref2";
import { paged } from "./helpers";

// Evidence and guidelines (reference: Anticipa Evidence & Guidelines Dashboard), route /evidence-review.
const SECOND = "Father has no history of colorectal cancer";

function payload() {
  const p = journeyPayload() as ReturnType<typeof journeyPayload> & { evidence: { facts: unknown[] } };
  const text = DOCS.en.text;
  const start = Array.from(text.slice(0, text.indexOf(SECOND))).length;
  const second = factFor("en", {
    id: "77777777-0000-4000-8000-0000000000e4",
    fact_type: "colorectal_cancer",
    relationship_type: "father",
    assertion_status: "negative",
    value: { original_text: SECOND },
    sources: [{ id: "88888888-0000-4000-8000-0000000000e4", document_id: DOCS.en.id, quote: SECOND, start_offset: start, end_offset: start + Array.from(SECOND).length, verification_status: "verified_exact", location_ambiguous: false }],
  });
  p.evidence.facts.push(second);
  return p;
}

const handlers = () => ({
  ...journeyHandlers(payload()),
  "GET /evidence": () => ({ body: paged([factFor("en"), factFor("ar")], 1, 40, 2) }),
  "GET /guidelines": () => ({ body: paged(guidelinesList()) }),
});

test("evidence extraction: the note with numbered highlights, evidence list, matched guideline and decision", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/evidence-review");
  await expect(page.getByRole("heading", { level: 1, name: "Evidence and guidelines" })).toBeVisible();
  const note = page.getByRole("region", { name: "Clinical note with extracted facts highlighted" });
  await expect(note.locator('mark[data-fact-n="1"]')).toContainText(DOCS.en.quote);
  await expect(note.locator('mark[data-fact-n="2"]')).toContainText(SECOND);
  await expect(page.getByText("1 / 3")).toBeVisible();
  const facts = page.getByRole("list", { name: "Extracted facts" });
  await expect(facts.getByRole("listitem")).toHaveCount(2);
  await expect(facts.getByRole("listitem").first()).toContainText("Breast cancer");
  await expect(facts.getByRole("listitem").first()).toContainText("Risk factor");
  await expect(facts.getByRole("listitem").nth(1)).toContainText("Negated");
  await expect(facts.getByRole("listitem").first()).toContainText("Quote verified");
  const match = page.getByRole("region", { name: "Guideline matching" });
  await expect(match).toContainText("Breast cancer screening");
  await expect(match).toContainText("Matched");
  await expect(match).toContainText("Age 50 to 74");
  await expect(page.getByRole("region", { name: "Care decision" })).toContainText("Breast cancer screening recommended");
  const pipe = page.getByRole("region", { name: "From record to action" });
  await expect(pipe).toContainText("3 unstructured notes");
  await expect(page.getByText(/synthetic|pending clinical review/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.screenshot({ path: "test-results/screenshots/ref-evidence.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-evidence-full.png", fullPage: true });
  // The next note is the Arabic one: right to left, its own highlight.
  await page.getByRole("button", { name: "Next note" }).click();
  await expect(page.getByText("2 / 3")).toBeVisible();
  const ar = page.getByRole("region", { name: "Clinical note with extracted facts highlighted" });
  await expect(ar).toHaveAttribute("dir", "rtl");
  await expect(ar.locator("mark")).toContainText(DOCS.ar.quote);
});

test("tabs: the review queue keeps its function and Guidelines opens the library", async ({ page, context }) => {
  await bootB(page, context, "population_admin", handlers());
  await page.goto("/evidence-review");
  await page.getByRole("tab", { name: "Review queue" }).click();
  await expect(page.getByRole("heading", { name: /Review queue \(/ })).toBeVisible();
  await page.getByRole("tab", { name: "Guidelines" }).click();
  await expect(page).toHaveURL(/\/guidelines$/);
});

test("no evidence yet: an honest empty state", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...handlers(), "GET /evidence": () => ({ body: paged([], 1, 40, 0) }) });
  await page.goto("/evidence-review");
  await expect(page.getByText("No evidence has been extracted yet")).toBeVisible();
});

for (const w of [1440, 390]) {
  test(`evidence at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await bootB(page, context, "observer", handlers());
    await page.goto("/evidence-review");
    await expect(page.getByRole("list", { name: "Extracted facts" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}


test("selecting a fact finds it in the note and filters dim the rest", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/evidence-review");
  const note = page.getByRole("region", { name: "Clinical note with extracted facts highlighted" });
  await page.getByRole("list", { name: "Extracted facts" }).getByRole("button").nth(1).click();
  await expect(note.locator('mark[data-fact-n="2"]')).toHaveAttribute("data-selected", "true");
  // Clicking the highlight selects its row.
  await note.locator('mark[data-fact-n="1"]').click();
  await expect(page.getByRole("list", { name: "Extracted facts" }).getByRole("button").first()).toHaveAttribute("aria-pressed", "true");
  // The Negated filter keeps one row and dims the other highlight.
  await page.getByRole("button", { name: /^Negated/ }).click();
  await expect(page.getByRole("list", { name: "Extracted facts" }).getByRole("listitem")).toHaveCount(1);
  await expect(note.locator('mark[data-fact-n="1"]')).toHaveClass(/opacity-50/);
  await page.getByRole("button", { name: /^Needs review/ }).click();
  await expect(page.getByText("No fact in this note matches the filter.")).toBeVisible();
});
