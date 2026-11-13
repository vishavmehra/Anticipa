import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { CASE_ID } from "./fixtures";
import { conversationHandlers } from "./conversation-fixtures";
import { CASE_B, R_ID, bootB, expectNoDashes, journeyHandlers } from "./features-fixtures";
import { MODULE_HANDLERS } from "./fixtures";

// <CaseConversation /> mounted in the resident journey ACT panel and on the care gap detail page. API calls are route mocked.
// Reading needs engagement.view and posting needs caregaps.manage (backend/anticipa/conversation/routes.py).

const journey = async (page: Page) => {
  await page.goto(`/residents/${R_ID}/journey`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Journey of resident");
};
const conversation = (page: Page) => page.getByRole("region", { name: "Conversation" });

test("journey ACT panel shows the conversation right after the reminder preview and lets a coordinator post", async ({ page, context }) => {
  await bootB(page, context, "population_admin", journeyHandlers());
  await journey(page);
  const card = conversation(page);
  await expect(card).toBeVisible();
  await expect(card.getByRole("log", { name: "Conversation thread" }).getByRole("listitem")).toHaveCount(2);
  const afterPreview = await page.evaluate(() => {
    const preview = document.querySelector('[aria-label="Reminder in each language"]');
    const conv = document.querySelector('[aria-label="Conversation thread"]');
    return !!preview && !!conv && !!(preview.compareDocumentPosition(conv) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  expect(afterPreview).toBe(true);
  await expect(card.getByRole("textbox")).toBeVisible();
  await expectNoDashes(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/screenshots/ref-journey-act-conversation.png" });
});

test("journey: posting a message sends the typed text and shows the assistant reply", async ({ page, context }) => {
  const posted: unknown[] = [];
  await bootB(page, context, "population_admin", { ...journeyHandlers(), ...conversationHandlers(CASE_B, posted) });
  await journey(page);
  const card = conversation(page);
  await card.getByRole("textbox").fill("Where do I go on the day?");
  await card.getByRole("button", { name: /send/i }).click();
  await expect(card.getByRole("log", { name: "Conversation thread" }).getByText("Where do I go on the day?")).toBeVisible();
  await expect(card.getByRole("log", { name: "Conversation thread" }).getByRole("listitem")).toHaveCount(4);
  expect(posted).toEqual([{ text: "Where do I go on the day?", language: "en" }]);
});

test("an observer reads the thread without the message box", async ({ page, context }) => {
  await bootB(page, context, "observer", journeyHandlers());
  await journey(page);
  const card = conversation(page);
  await expect(card.getByRole("log", { name: "Conversation thread" })).toBeVisible();
  await expect(card.getByRole("textbox")).toHaveCount(0);
});

test("a role without engagement.view never sees the conversation or asks for it", async ({ page, context }) => {
  const calls: string[] = [];
  await bootB(page, context, "clinical_reviewer", {
    ...journeyHandlers(),
    [`GET /care-gaps/${CASE_B}/conversation`]: () => {
      calls.push("conversation");
      return { status: 403, body: { error: { code: "FORBIDDEN", message: "No permission.", details: [], request_id: "r" } } };
    },
  });
  await journey(page);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(conversation(page)).toHaveCount(0);
  expect(calls).toEqual([]);
});

test("care gap detail shows the conversation between outreach and appointments", async ({ page, context }) => {
  await bootB(page, context, "population_admin", { ...MODULE_HANDLERS, ...conversationHandlers(CASE_ID) });
  await page.goto(`/care-gaps/${CASE_ID}`);
  const card = conversation(page);
  await expect(card).toBeVisible();
  await expect(card.getByRole("log", { name: "Conversation thread" }).getByRole("listitem")).toHaveCount(2);
  const between = await page.evaluate(() => {
    const heads = [...document.querySelectorAll("h2")].map((h) => h.textContent?.trim());
    const conv = document.querySelector('[aria-label="Conversation thread"]');
    const outreach = [...document.querySelectorAll("h2")].find((h) => h.textContent?.trim() === "Outreach");
    const appts = [...document.querySelectorAll("h2")].find((h) => h.textContent?.trim() === "Appointments");
    return { heads, ok: !!conv && !!outreach && !!appts && !!(outreach.compareDocumentPosition(conv) & Node.DOCUMENT_POSITION_FOLLOWING) && !!(conv.compareDocumentPosition(appts) & Node.DOCUMENT_POSITION_FOLLOWING) };
  });
  expect(between.ok).toBe(true);
  await expectNoDashes(page);
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/screenshots/ref-care-gap-detail-conversation.png" });
});

for (const [path, name] of [[`/residents/${R_ID}/journey`, "journey"], [`/care-gaps/${CASE_ID}`, "care gap detail"]] as const) {
  for (const w of [1440, 390]) {
    test(`${name} with the conversation at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
      await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
      await bootB(page, context, "population_admin", { ...MODULE_HANDLERS, ...journeyHandlers(), ...conversationHandlers(CASE_ID) });
      await page.goto(path);
      await expect(conversation(page)).toBeVisible();
      await expect(page.locator(".skeleton")).toHaveCount(0, { timeout: 15_000 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
      const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
      expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
    });
  }
}
