import AxeBuilder from "@axe-core/playwright";
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { errorBody, ok } from "./helpers";

// <CaseConversation /> on its test host (/dev/conversation) with the API route mocked. Run with --workers=1.
// Messages below are invented test text; the approved reply wording is copied from backend/anticipa/conversation/safety.py.

const CASE = "eeeeeeee-0000-4000-8000-0000000000c1";
const GET = `GET /care-gaps/${CASE}/conversation`;
const POST = `POST /care-gaps/${CASE}/conversation/messages`;
const HI_CLINICIAN = "आपके संदेश के लिए धन्यवाद। चिकित्सा संबंधी प्रश्नों का उत्तर देना मेरे दायरे में नहीं है। मैंने आपका प्रश्न आपकी देखभाल टीम के एक चिकित्सक को भेज दिया है, जो आपसे संपर्क करेंगे।";
const AR_CLINICIAN = "شكراً لرسالتك. لا يمكنني الإجابة عن الأسئلة الطبية. لقد أحلت سؤالك إلى طبيب من فريق رعايتك، وسيتواصل معك.";
const EN_CLINICIAN = "Thank you for your message. I cannot answer medical questions. I have passed your question to a clinician from your care team, who will follow up with you.";

let n = 0;
const at = () => new Date(Date.UTC(2026, 10, 13, 9, 0, ++n)).toISOString();
const msg = (over: Record<string, unknown>) => ({
  id: `aaaaaaaa-0000-4000-8000-${String(1000 + n).padStart(12, "0")}`, role: "resident", body: "", language: "en", direction: "ltr", intent: null, source: "resident_input",
  escalation_state: null, escalation_reason: null, proposal: null, model_id: null, created_at: at(), synthetic_status: "simulated", simulated: true, marker: null, reply_sent: null, ...over,
});
const assistant = (over: Record<string, unknown>) => msg({ role: "assistant", source: "template", marker: "[SIMULATED]", reply_sent: null, ...over });

const PROPOSAL = { ref: "s1", slot_id: "bbbbbbbb-0000-4000-8000-000000000001", date: "Monday 23 November 2026", start: "09:00", end: "09:30", timezone: "Asia/Kolkata", clinic: "Test Clinic", address: null, service: "Screening", status: "proposed", requires_coordinator_confirmation: true };

function fullThread() {
  return [
    msg({ body: "When is my appointment and where?", intent: "location_time", reply_sent: true }),
    assistant({ body: "Your appointment is on Friday 20 November 2026 at 10:15 at Test Clinic. Please arrive a little early.", source: "model", intent: "location_time", model_id: "claude-haiku-5-5" }),
    msg({ body: "Can I come on a different day?", intent: "rescheduling", reply_sent: true }),
    assistant({ body: "I can suggest Monday 23 November 2026 at 09:00 at Test Clinic. A care coordinator will confirm it with you.", source: "model", intent: "rescheduling", proposal: PROPOSAL }),
    msg({ body: "मेरे सीने में गांठ है, क्या यह कैंसर है?", language: "hi", intent: "medical", escalation_reason: "medical_question", escalation_state: "escalated", reply_sent: true }),
    assistant({ body: HI_CLINICIAN, language: "hi", intent: "medical" }),
    msg({ body: "عندي كتلة في الثدي", language: "ar", direction: "rtl", intent: "medical", escalation_reason: "medical_question", escalation_state: "already_escalated", reply_sent: true }),
    assistant({ body: AR_CLINICIAN, language: "ar", direction: "rtl", intent: "medical" }),
    msg({ body: "Please stop sending me messages", intent: "opt_out", reply_sent: true }),
    assistant({ body: "Understood. We will not send you further reminders about this. If you change your mind, please tell your clinic or care team.", intent: "opt_out" }),
    msg({ body: "When is my appointment?", intent: "scheduling", reply_sent: false }),
  ];
}

const thread = (messages: unknown[], over: Record<string, unknown> = {}) => ({
  care_gap_case_id: CASE, case_status: "escalated", resident_language: "en", escalated: true, consent: { denied_channels: ["sms"], outreach_blocked: false, ai_processing_denied: false },
  messages, simulated: true, notice: "Simulated conversation: no message was sent to a real person and nothing was booked.", ...over,
});
const result = (resident: unknown, reply: unknown, over: Record<string, unknown> = {}) => ({
  resident_message: resident, reply, suppressed: false, suppressed_reason: null, channel: "sms", escalation: null, opt_out: null, case_status: "planned", cost_usd: 0.0001, latency_ms: 1400, simulated: true, notice: "Simulated.", ...over,
});

const host = (query = "edit=1") => `/dev/conversation?case=${CASE}&${query}`;
const region = (page: Page) => page.getByRole("region", { name: "Conversation" });
const log = (page: Page) => page.getByRole("log", { name: "Conversation thread" });

async function open(page: Page, context: BrowserContext, handlers: Record<string, (req: import("@playwright/test").Request) => { status?: number; body: unknown } | Promise<{ status?: number; body: unknown }>>, query?: string) {
  const api = await bootB(page, context, "population_admin", handlers);
  await page.goto(host(query));
  await expect(region(page)).toBeVisible();
  return api;
}

test("the thread shows both sides with language, direction, simulated labels, hand off chips and a proposed slot that is not a booking", async ({ page, context }) => {
  await open(page, context, { [GET]: () => ({ body: ok(thread(fullThread())) }) }, "");
  const items = log(page).getByRole("listitem");
  await expect(items).toHaveCount(11);
  await expect(region(page).getByText("Simulated conversation")).toBeVisible();
  await expect(items.first().getByText("Simulated", { exact: true })).toBeVisible();
  await expect(items.nth(1)).toContainText("Written by the assistant");
  await expect(items.nth(5)).toContainText("Approved text");

  const proposal = items.nth(3);
  await expect(proposal).toContainText("Proposed slot: Monday 23 November 2026, 09:00 to 09:30 at Test Clinic");
  await expect(proposal).toContainText("Not booked. A care coordinator must confirm it with the resident.");

  await expect(items.nth(4)).toContainText("Medical question, escalated to a clinician");
  await expect(items.nth(6)).toContainText("Medical question, already escalated");
  await expect(items.nth(8)).toContainText("Opt out recorded");
  await expect(items.nth(10)).toContainText("Reply not sent, resident opted out of this channel");
  await expect(region(page).getByText("Case escalated, waiting for a clinician")).toBeVisible();
  await expect(region(page).getByRole("status").filter({ hasText: "opted out of SMS" })).toContainText("No replies are sent on that channel.");

  // language and direction of every body: Arabic reads right to left, Hindi keeps its script
  await expect(log(page).locator("p[lang=ar][dir=rtl]")).toHaveCount(2);
  await expect(log(page).locator("p[lang=hi]")).toHaveCount(2);
  await expect(log(page).locator("p[lang=en][dir=ltr]").first()).toBeVisible();

  // read only access: no message box
  await expect(region(page).getByRole("textbox")).toHaveCount(0);
  await expect(region(page)).toContainText("Read only access: you can follow this conversation but not add messages.");
  await expectNoDashes(page);
  const scan = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(scan.violations.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual([]);
  await page.screenshot({ path: "test-results/screenshots/conversation-1440.png", fullPage: true });
});

test("a coordinator picks a language and a channel, types a message, sees it while the reply is prepared, then the reply", async ({ page, context }) => {
  const server = [msg({ body: "earlier question", intent: "location_time", reply_sent: true }), assistant({ body: "Your appointment is on Friday 20 November 2026 at 10:15 at Test Clinic.", source: "model" })];
  const posted: { text: string; language?: string; channel?: string }[] = [];
  await open(page, context, {
    [GET]: () => ({ body: ok(thread(server, { escalated: false, case_status: "planned", consent: { denied_channels: [], outreach_blocked: false, ai_processing_denied: false } })) }),
    [POST]: async (req) => {
      const body = req.postDataJSON();
      posted.push(body);
      await new Promise((r) => setTimeout(r, 700));
      const resident = msg({ body: body.text, language: body.language, direction: "rtl", intent: "scheduling", reply_sent: true });
      const reply = assistant({ body: "موعدك يوم 20 نوفمبر 2026 الساعة 10:15.", language: "ar", direction: "rtl", source: "model", intent: "scheduling" });
      server.push(resident, reply);
      return { status: 201, body: ok(result(resident, reply, { channel: "whatsapp" })) };
    },
  });
  const form = region(page).getByRole("form", { name: "Add a resident message" });
  await expect(form.getByRole("radio", { name: "English" })).toBeChecked();
  const box = form.getByRole("textbox", { name: "Resident message" });
  await expect(box).toHaveAttribute("dir", "ltr");
  await expect(form.getByRole("button", { name: "Send as resident" })).toBeDisabled();

  await form.getByText("AR", { exact: true }).click();
  await expect(form.getByRole("radio", { name: "Arabic" })).toBeChecked();
  await expect(box).toHaveAttribute("dir", "rtl");
  await expect(box).toHaveAttribute("lang", "ar");
  await expect(form.getByRole("button", { name: "متى موعدي وأين؟" })).toBeVisible(); // examples follow the language
  await form.getByRole("button", { name: "متى موعدي وأين؟" }).click();
  await expect(box).toHaveValue("متى موعدي وأين؟");
  await form.getByLabel("Channel the resident wrote on").selectOption("whatsapp");
  // the selected language pill must be readable: white text on the brand colour, not on the glass
  const selected = await form.getByText("AR", { exact: true }).evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(selected).not.toBe("rgba(0, 0, 0, 0)");
  const scan = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(scan.violations.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual([]);
  await form.getByRole("button", { name: "Send as resident" }).click();

  await expect(log(page).getByRole("listitem", { name: "Sending" })).toContainText("Preparing a reply");
  await expect(log(page).getByRole("listitem", { name: "Sending" })).toContainText("متى موعدي وأين؟");
  await expect(log(page).getByText("موعدك يوم 20 نوفمبر 2026 الساعة 10:15.")).toBeVisible();
  expect(posted).toEqual([{ text: "متى موعدي وأين؟", language: "ar", channel: "whatsapp" }]);
  await expect(box).toHaveValue("");
  await expect(log(page).getByRole("listitem")).toHaveCount(4);
  await expect(region(page).getByRole("status").filter({ hasText: "The assistant replied." })).toBeVisible();
  await page.screenshot({ path: "test-results/screenshots/conversation-compose-1440.png", fullPage: true });
});

test("a health question is handed to a clinician: the approved text, an escalation chip and the case banner appear", async ({ page, context }) => {
  const server: unknown[] = [];
  await open(page, context, {
    [GET]: () => ({ body: ok(thread(server, { escalated: server.length > 0, case_status: server.length ? "escalated" : "planned", consent: { denied_channels: [], outreach_blocked: false, ai_processing_denied: false } })) }),
    [POST]: (req) => {
      const body = req.postDataJSON();
      const resident = msg({ body: body.text, intent: "emergency", escalation_reason: "emergency", escalation_state: "escalated", reply_sent: true });
      const reply = assistant({ body: EN_CLINICIAN, intent: "emergency" });
      server.push(resident, reply);
      return { status: 201, body: ok(result(resident, reply, { escalation: { reason: "emergency", state: "escalated", urgent: true }, case_status: "escalated" })) };
    },
  });
  await expect(log(page).getByText("No messages yet")).toBeVisible();
  const box = region(page).getByRole("textbox", { name: "Resident message" });
  await box.fill("I have severe chest pain");
  await box.press("Enter"); // Enter sends
  const items = log(page).getByRole("listitem");
  await expect(items).toHaveCount(2);
  await expect(items.first()).toContainText("Emergency, escalated to a clinician");
  await expect(items.nth(1)).toContainText("I cannot answer medical questions");
  await expect(items.nth(1)).toContainText("Approved text");
  await expect(region(page).getByText("Case escalated, waiting for a clinician")).toBeVisible();
  await expect(region(page).getByRole("status").filter({ hasText: "Emergency, escalated to a clinician." })).toBeVisible();
});

test("when the resident opted out of the channel the message is kept, no reply appears and the screen says so", async ({ page, context }) => {
  const server: unknown[] = [];
  await open(page, context, {
    [GET]: () => ({ body: ok(thread(server, { escalated: false, case_status: "planned", consent: { denied_channels: ["sms"], outreach_blocked: false, ai_processing_denied: false } })) }),
    [POST]: (req) => {
      const resident = msg({ body: req.postDataJSON().text, intent: "scheduling", reply_sent: false });
      server.push(resident);
      return { status: 201, body: ok(result(resident, null, { suppressed: true, suppressed_reason: "consent_denied", channel: "sms" })) };
    },
  });
  await expect(region(page).getByRole("status").filter({ hasText: "opted out of SMS" })).toBeVisible();
  const box = region(page).getByRole("textbox", { name: "Resident message" });
  await box.fill("When is my appointment?");
  await region(page).getByRole("button", { name: "Send as resident" }).click();
  const items = log(page).getByRole("listitem");
  await expect(items).toHaveCount(1);
  await expect(items.first()).toContainText("Reply not sent, resident opted out of this channel");
  await expect(region(page).getByRole("status").filter({ hasText: "Reply not sent: the resident opted out of SMS." })).toBeVisible();
});

test("every channel denied shows one clear notice", async ({ page, context }) => {
  await open(page, context, { [GET]: () => ({ body: ok(thread([], { consent: { denied_channels: ["sms", "whatsapp", "phone_call", "email"], outreach_blocked: true, ai_processing_denied: false } })) }) });
  await expect(region(page)).toContainText("The resident opted out of outreach on every channel. No replies are sent.");
});

test("Shift Enter adds a line and does not send; the box stops at 600 characters; a failed send keeps the text and says why", async ({ page, context }) => {
  let calls = 0;
  await open(page, context, {
    [GET]: () => ({ body: ok(thread([], { escalated: false })) }),
    [POST]: () => {
      calls++;
      return errorBody("RATE_LIMITED", "Message limit reached (60 per hour). Try again later.", 429);
    },
  });
  const box = region(page).getByRole("textbox", { name: "Resident message" });
  await box.fill("first line");
  await box.press("Shift+Enter");
  await box.type("second line");
  expect(calls).toBe(0);
  await expect(box).toHaveValue("first line\nsecond line");
  await box.fill("x".repeat(640));
  await expect(box).toHaveValue("x".repeat(600));
  await expect(region(page).getByText("0 characters left")).toBeVisible();
  await box.fill("I want to send this");
  await region(page).getByRole("button", { name: "Send as resident" }).click();
  await expect(region(page).getByRole("alert")).toContainText("The message limit has been reached for now. Please try again later.");
  expect(calls).toBe(1);
  await expect(box).toHaveValue("I want to send this");
  await expect(region(page).getByRole("button", { name: "Send as resident" })).toBeEnabled();
});

test("the thread failing to load shows an error with a retry, not an empty conversation", async ({ page, context }) => {
  let tries = 0;
  await open(page, context, {
    [GET]: () => (++tries === 1 ? errorBody("INTERNAL_ERROR", "boom", 500) : { body: ok(thread(fullThread().slice(0, 2), { escalated: false })) }),
  });
  await expect(region(page).getByText("Could not load the conversation")).toBeVisible();
  await region(page).getByRole("button", { name: /Try again|Retry/ }).click();
  await expect(log(page).getByRole("listitem")).toHaveCount(2);
});

test("on a phone the card stays inside the screen and the message box is usable", async ({ page, context }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, context, { [GET]: () => ({ body: ok(thread(fullThread())) }) });
  await expect(log(page).getByRole("listitem")).toHaveCount(11);
  await expect(region(page).getByRole("textbox", { name: "Resident message" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: "test-results/screenshots/conversation-390.png", fullPage: true });
});
