import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { bootB } from "./features-fixtures";
import { MODULE_HANDLERS } from "./fixtures";
import { errorBody, ok } from "./helpers";

// Ask Anticipa (<AskAnticipa />), exercised in the app shell (top bar button) on the Command Center with the API mocked. Run with --workers=1.
// All figures below are invented demo values for tests only: they are not data from the system.

const BAR_ANSWER = {
  answer: "Pune (D05) has the lowest closure rate at 0.6%, against 0.8% in Varanasi (D08), a gap of 0.2 points. Closure rate is completed cases divided by cases opened.",
  chart: {
    kind: "bar", title: "Closure rate by district", x: "label", y: "closure_rate", x_label: "District", y_label: "Closure rate", unit: "ratio", source_call: "c1", source: "simulated",
    data: [{ x: "Varanasi (D08)", y: 0.0079 }, { x: "Bhopal (D01)", y: 0.0061 }, { x: "Pune (D05)", y: 0.0055 }],
  },
  table: {
    title: "Cases and closure by district", source_call: "c1", source: "simulated",
    columns: [{ key: "label", label: "District" }, { key: "cases", label: "Cases opened", unit: "count" }, { key: "closure_rate", label: "Closure rate", unit: "ratio" }],
    rows: [{ label: "Varanasi (D08)", cases: 11500, closure_rate: 0.0079 }, { label: "Bhopal (D01)", cases: 11600, closure_rate: 0.0061 }, { label: "Pune (D05)", cases: 11820, closure_rate: 0.0055 }],
  },
  sources: [{ call_id: "c1", tool: "get_geography", args: { level: "district" }, definition: "closure_rate = completed / cases opened. Groups with fewer than 5 cases are suppressed.", source: "simulated" }],
  follow_ups: ["How full is clinic capacity in Pune?", "Which programme closes fastest in Pune?"],
  grounded: true, refused: false, notice: null, model: "claude-haiku-5-5", cost_usd: 0.0003, latency_ms: 3400, cached: false, iterations: 2,
  synthetic_banner: "Demonstration data only: every record is generated or simulated, and no real person is represented.",
};
const TREND_ANSWER = {
  ...BAR_ANSWER, answer: "Closures rose over the last week.",
  chart: { kind: "line", title: "Cases closed per day", x: "date", y: "closed", x_label: "Date", y_label: "Closed", unit: "count", source_call: "c1", source: "simulated",
           data: [{ x: "2026-11-06", y: 0 }, { x: "2026-11-07", y: 12 }, { x: "2026-11-08", y: 30 }] },
  table: null, follow_ups: [],
};
const REFUSAL = { ...BAR_ANSWER, answer: "I cannot give medical advice. I can show closure rates by district.", chart: null, table: null, sources: [], refused: true, follow_ups: ["Which district has the lowest closure rate?"] };
const UNVERIFIED = { ...BAR_ANSWER, grounded: false, notice: "Some figures in this answer could not be verified against the data.", answer: "Pune closed 9.9% of cases." , follow_ups: [] };

const SUGGESTIONS = { suggestions: [
  { question: "Why is the closure rate lowest in Pune, and how does it compare with other districts?", topic: "equity" },
  { question: "What is the closure rate and planning coverage for cervical cancer screening?", topic: "programmes" },
  { question: "How full is clinic capacity in Bengaluru Urban (D09) over the next two weeks?", topic: "capacity" },
  { question: "How many care gaps are open, and how many are waiting for outreach?", topic: "overview" },
] };

type Posted = { question: string; history: { role: string; content: string }[] };

async function open(page: Page, context: import("@playwright/test").BrowserContext, reply: (q: string, n: number) => { status?: number; body: unknown } | Promise<{ status?: number; body: unknown }>, delayMs = 0) {
  const posted: Posted[] = [];
  await bootB(page, context, "observer", {
    ...MODULE_HANDLERS,
    "GET /ask/suggestions": () => ({ body: ok(SUGGESTIONS) }),
    "POST /ask": async (req) => {
      const body = req.postDataJSON() as Posted;
      posted.push(body);
      if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
      return reply(body.question, posted.length);
    },
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Ask Anticipa" })).toBeVisible();
  // wait for hydration: the server rendered button is visible before React has attached the Control J listener
  await page.waitForFunction(() => Object.keys(document.querySelector("button[aria-label=\"Ask Anticipa\"]")!).some((k) => k.startsWith("__reactProps")));
  return posted;
}
const panel = (page: Page) => page.getByRole("dialog", { name: "Ask Anticipa" });
const answerAll = () => ({ body: ok(BAR_ANSWER) });

test("opens with Control J, shows live suggestions, answers with chart, table and sources, then a follow up keeps context", async ({ page, context }) => {
  const posted = await open(page, context, answerAll, 700);
  await page.keyboard.press("Control+J");
  await expect(panel(page)).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Your question" })).toBeFocused();
  const chips = panel(page).getByRole("list").first().getByRole("button");
  await expect(chips).toHaveCount(4);
  await expect(chips.first()).toContainText("closure rate lowest in Pune");

  await chips.first().click();
  await expect(page.getByRole("status").filter({ hasText: "Looking at the data" })).toBeVisible(); // the fast spinner
  const answer = panel(page).getByRole("article");
  await expect(answer.getByText("Pune (D05) has the lowest closure rate at 0.6%")).toBeVisible();
  expect(posted[0].question).toContain("closure rate lowest in Pune");
  expect(posted[0].history).toEqual([]);

  // chart and table are built from the server's data, formatted by unit
  const chart = answer.getByRole("list", { name: "Closure rate by district" });
  await expect(chart.getByRole("listitem")).toHaveCount(3);
  await expect(chart).toContainText("0.79%");
  await expect(chart).toContainText("0.55%");
  const table = answer.getByRole("table", { name: "Cases and closure by district" });
  await expect(table).toContainText("11,820");
  await expect(table.getByRole("columnheader", { name: "Closure rate" })).toBeVisible();

  // How I got this
  await answer.getByText("How I got this").click();
  await expect(answer).toContainText("Districts and states");
  await expect(answer).toContainText("Simulated data");
  await expect(answer).toContainText("completed / cases opened");
  await expect(answer).toContainText("Filters used: level: district");
  await expect(answer).toContainText("Every number was matched against the data");

  // follow up chip asks again with the earlier exchange as history
  await answer.getByRole("button", { name: "How full is clinic capacity in Pune?" }).click();
  await expect(panel(page).getByRole("article")).toHaveCount(2);
  await expect.poll(() => posted.length).toBe(2);
  expect(posted[1].question).toBe("How full is clinic capacity in Pune?");
  expect(posted[1].history.map((h) => h.role)).toEqual(["user", "assistant"]);
  expect(posted[1].history[1].content).toContain("lowest closure rate");
  await page.screenshot({ path: "test-results/screenshots/ask-1440.png" });
});

test("typing a question: Enter sends, Shift Enter does not, the box validates length, Escape closes and returns focus", async ({ page, context }) => {
  const posted = await open(page, context, answerAll);
  const trigger = page.getByRole("button", { name: /Ask Anticipa/ });
  await trigger.click();
  const box = page.getByRole("textbox", { name: "Your question" });
  await box.fill("hi");
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeDisabled(); // under 3 characters
  await box.fill("Which district has the lowest closure rate?");
  await box.press("Shift+Enter");
  expect(posted).toHaveLength(0);
  await box.press("Enter");
  await expect(panel(page).getByRole("article")).toHaveCount(1);
  expect(posted[0].question).toBe("Which district has the lowest closure rate?\n".trim());
  await expect(box).toHaveValue("");
  await box.fill("x".repeat(520));
  await expect(box).toHaveValue("x".repeat(500));
  await expect(panel(page).getByText("0 characters left")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(panel(page)).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await page.keyboard.press("Control+J"); // the shortcut toggles, and the conversation was kept? no: the panel remounts empty
  await expect(panel(page)).toBeVisible();
  await page.keyboard.press("Control+J");
  await expect(panel(page)).toHaveCount(0);
});

test("focus stays inside the open panel (Tab and Shift Tab wrap)", async ({ page, context }) => {
  await open(page, context, answerAll);
  await page.keyboard.press("Control+J");
  const close = panel(page).getByRole("button", { name: "Close Ask Anticipa" });
  await close.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(panel(page).getByRole("textbox", { name: "Your question" })).toBeFocused(); // wrapped to the last enabled control (Ask is disabled while the box is empty)
  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();
});

test("a line chart for a trend; a refusal explains the limit without a chart", async ({ page, context }) => {
  await open(page, context, (q) => ({ body: ok(q.includes("trend") ? TREND_ANSWER : REFUSAL) }));
  await page.keyboard.press("Control+J");
  const box = page.getByRole("textbox", { name: "Your question" });
  await box.fill("Show the closure trend this week");
  await box.press("Enter");
  const first = panel(page).getByRole("article").first();
  await expect(first.getByRole("img", { name: "Cases closed per day" })).toBeVisible();
  await box.fill("What medicine should I take?");
  await box.press("Enter");
  const second = panel(page).getByRole("article").nth(1);
  await expect(second.getByText("This is outside what Ask Anticipa can answer")).toBeVisible();
  await expect(second).toContainText("I cannot give medical advice");
  await expect(second.getByRole("list", { name: /closure/i })).toHaveCount(0);
  await expect(second.getByText("How I got this")).toHaveCount(0);
});

test("an answer whose figures could not be verified says so and hides its text behind a disclosure", async ({ page, context }) => {
  await open(page, context, () => ({ body: ok(UNVERIFIED) }));
  await page.keyboard.press("Control+J");
  await panel(page).getByRole("textbox", { name: "Your question" }).fill("How many cases did Pune close?");
  await page.keyboard.press("Enter");
  const a = panel(page).getByRole("article");
  await expect(a.getByText("Could not verify this answer")).toBeVisible();
  await expect(a.getByText("Pune closed 9.9% of cases.")).toBeHidden(); // inside a closed <details>
  await a.getByText("Show the unverified text").click();
  await expect(a.getByText("Pune closed 9.9% of cases.")).toBeVisible();
  await expect(a.getByRole("list", { name: "Closure rate by district" })).toBeVisible(); // data from the tools is still shown
});

test("errors: provider outage offers Try again; the rate limit and a forbidden role get plain messages", async ({ page, context }) => {
  let calls = 0;
  await open(page, context, (q) => {
    calls++;
    if (q.includes("limit")) { const e = errorBody("RATE_LIMITED", "Too many requests", 429); return { status: 429, body: e.body }; }
    if (q.includes("forbidden")) { const e = errorBody("FORBIDDEN", "no", 403); return { status: 403, body: e.body }; }
    if (calls === 1) { const e = errorBody("PROVIDER_UNAVAILABLE", "down", 503); return { status: 503, body: e.body }; }
    return { body: ok(BAR_ANSWER) };
  });
  await page.keyboard.press("Control+J");
  const box = page.getByRole("textbox", { name: "Your question" });
  await box.fill("Which district is lowest?");
  await box.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "temporarily unavailable" })).toBeVisible();
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(panel(page).getByText("Pune (D05) has the lowest closure rate")).toBeVisible();
  await box.fill("one more over the limit");
  await box.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "question limit has been reached" })).toBeVisible();
  await box.fill("a forbidden question here");
  await box.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "does not include access" })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "does not include access" }).getByRole("button", { name: "Try again" })).toHaveCount(0);
});

test("empty state falls back to generic questions when suggestions fail", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...MODULE_HANDLERS, "GET /ask/suggestions": () => ({ status: 503, body: errorBody("PROVIDER_UNAVAILABLE", "x", 503).body }) });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Ask Anticipa" })).toBeVisible();
  await page.waitForFunction(() => Object.keys(document.querySelector("button[aria-label=\"Ask Anticipa\"]")!).some((k) => k.startsWith("__reactProps")));
  await page.keyboard.press("Control+J");
  await expect(panel(page).getByRole("button", { name: "Which district has the lowest closure rate?" })).toBeVisible();
  await expect(panel(page)).toContainText("does not give medical advice");
});

for (const w of [1440, 390]) {
  test(`at ${w}px: axe clean, no dashes, no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await open(page, context, answerAll);
    await page.keyboard.press("Control+J");
    await panel(page).getByRole("button", { name: /closure rate lowest/ }).click();
    await expect(panel(page).getByRole("list", { name: "Closure rate by district" })).toBeVisible();
    await panel(page).getByText("How I got this").click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
    const bad = await page.evaluate(() => {
      const root = document.querySelector('[role="dialog"]')!.cloneNode(true) as HTMLElement;
      root.querySelectorAll("svg, kbd").forEach((e) => e.remove());
      const text = root.textContent ?? "";
      return [...text.matchAll(/[–—]|(^|\s)-(\s|$)|[A-Za-z]-[A-Za-z]/g)].map((m) => text.slice(Math.max(0, (m.index ?? 0) - 30), (m.index ?? 0) + 30));
    });
    expect(bad).toEqual([]);
    await page.screenshot({ path: `test-results/screenshots/ask-${w}.png` });
  });
}

test("launcher tooltip: shown on hover and on keyboard focus, then a click opens the panel", async ({ page, context }) => {
  await open(page, context, answerAll);
  const launcher = page.getByRole("button", { name: "Ask Anticipa" });
  const tip = page.getByRole("tooltip");
  await expect(tip).toHaveCSS("opacity", "0");
  await launcher.hover();
  await expect(tip).toHaveCSS("opacity", "1");
  await expect(tip).toContainText("Ask Anticipa");
  await page.mouse.move(5, 5);
  await expect(tip).toHaveCSS("opacity", "0");
  await launcher.focus();
  await expect(tip).toHaveCSS("opacity", "1");
  await launcher.click();
  await expect(panel(page)).toBeVisible();
});
