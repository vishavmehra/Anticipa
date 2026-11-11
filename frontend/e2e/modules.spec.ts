import { expect, test } from "@playwright/test";
import { ALLOC_ID, CASE_ID, DOCS, FACTS, GUIDELINE_ID, MODULE_HANDLERS, RES_ID, VERSION_ID } from "./fixtures";
import { alerts, boot, errorBody, ok, paged } from "./helpers";

const BANNER = "Synthetic data — not for clinical use";

test.describe("population", () => {
  test("shows measured counts with their basis and 'Not available' for a missing measurement", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Processing coverage" })).toBeVisible();
    await expect(page.getByText("Dataset syn-20261105-s42, as of 2026-11-05 09:14:31 UTC.")).toBeVisible();
    await expect(page.getByText("of 1,200 generated residents").first()).toBeVisible();
    // ai_processed_residents is null in the payload: it must read "Not available", never 0.
    const ai = page.getByText("AI evidence processed", { exact: true }).locator("xpath=../.."); // label -> header row -> tile
    await expect(ai).toContainText("Not available");
    await expect(page.getByRole("note")).toHaveText(BANNER);
  });
});

test.describe("residents", () => {
  test("list renders synthetic rows and sends the search query", async ({ page, context }) => {
    const searches: (string | null)[] = [];
    const { api } = await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      "GET /residents": (_r, url) => {
        searches.push(url.searchParams.get("search"));
        return MODULE_HANDLERS["GET /residents"](_r, url);
      },
    });
    await page.goto("/residents");
    const table = page.getByRole("table", { name: "Residents" });
    await expect(table.getByRole("row")).toHaveCount(4);
    await expect(table.getByText("Synthetic", { exact: true })).toHaveCount(3);
    await expect(table.getByText("Arabic")).toBeVisible();
    await expect(table.getByText("Hindi")).toBeVisible();
    await expect(page.getByText("1–3 of 1,200")).toBeVisible();

    await page.getByLabel("Search").fill("5a1c");
    await page.getByRole("button", { name: "Search" }).click();
    await expect.poll(() => searches.at(-1)).toBe("5a1c");
    expect(api.seen.find((s) => s.path === "/residents")?.headers.authorization).toMatch(/^Bearer /);
  });

  test("detail highlights the exact source span in English, Arabic (RTL) and Devanagari", async ({ page, context }) => {
    await boot(page, context, "clinical_reviewer", MODULE_HANDLERS);
    await page.goto(`/residents/${RES_ID}`);
    await expect(page.getByRole("heading", { name: `Resident ${RES_ID.slice(0, 8)}` })).toBeVisible();
    await expect(page.getByRole("table", { name: "Family relationships" }).getByRole("row")).toHaveCount(3);
    await expect(page.getByRole("table", { name: "Clinical records" }).getByRole("row")).toHaveCount(3);
    await expect(page.getByRole("list", { name: "Resident timeline" }).getByRole("listitem")).toHaveCount(2);
    await expect(page.getByRole("article")).toHaveCount(4);

    const buttons = page.getByRole("button", { name: "Show in document" });
    for (let i = 0; i < 3; i++) await buttons.first().click(); // each click turns that button into "Hide document context"
    await expect(page.locator("mark")).toHaveText([DOCS.en.quote, DOCS.ar.quote, DOCS.hi.quote]);
    await expect(page.locator('blockquote[lang="ar"][dir="rtl"]').filter({ has: page.locator("mark") })).toHaveCount(1);
    await expect(page.locator('blockquote[lang="hi"]').filter({ has: page.locator("mark") })).toHaveCount(1);
    // The caption reports offsets in the full document, even though only an excerpt is fetched.
    const enStart = DOCS.en.text.indexOf(DOCS.en.quote);
    await expect(page.getByText(`Characters ${enStart}–${enStart + DOCS.en.quote.length}`)).toBeVisible();
    // The fourth fact's quote was not found in its source: flagged, not hidden.
    await expect(page.getByText("could not be verified against its source text")).toBeVisible();
    await expect(page.getByRole("button", { name: "Accept" })).toHaveCount(0); // review actions live in the queue, not here
  });

  test("a missing resident shows the API error with its request id", async ({ page, context }) => {
    await boot(page, context, "population_admin", {
      [`GET /residents/${RES_ID}`]: () => ({ ...errorBody("NOT_FOUND", "The requested resource was not found.", 404) }),
    });
    await page.goto(`/residents/${RES_ID}`);
    await expect(alerts(page)).toContainText("The requested resource was not found.");
    await expect(alerts(page)).toContainText("req-test");
  });
});

test.describe("evidence review", () => {
  test("a reviewer accepts a verified fact; unverified facts cannot be accepted", async ({ page, context }) => {
    const { api } = await boot(page, context, "clinical_reviewer", {
      ...MODULE_HANDLERS,
      [`POST /evidence/${FACTS[0].id}/review`]: () => ({ body: ok({ id: FACTS[0].id, validation_status: "accepted" }) }),
    });
    await page.goto("/evidence-review");
    const cards = page.getByRole("article");
    await expect(cards).toHaveCount(4);
    // fact 4's only source is quote_not_found
    await expect(cards.nth(3).getByRole("button", { name: "Accept" })).toBeDisabled();
    await expect(cards.nth(3)).toContainText("Accept is unavailable until a quote is verified.");

    await cards.nth(0).getByLabel("Review note").fill("Matches the source.");
    await cards.nth(0).getByRole("button", { name: "Accept" }).click();
    await expect.poll(() => api.seen.filter((s) => s.method === "POST").length).toBe(1);
    const post = api.seen.find((s) => s.method === "POST")!;
    expect(post.path).toBe(`/evidence/${FACTS[0].id}/review`);
    expect(post.headers.authorization).toMatch(/^Bearer /);
  });

  test("rejecting needs a reason and sends the decision", async ({ page, context }) => {
    let body: unknown = null;
    const { api } = await boot(page, context, "clinical_reviewer", {
      ...MODULE_HANDLERS,
      [`POST /evidence/${FACTS[1].id}/review`]: (req) => {
        body = req.postDataJSON();
        return { body: ok({}) };
      },
    });
    await page.goto("/evidence-review");
    const card = page.getByRole("article").nth(1);
    await card.getByRole("button", { name: "Reject" }).click();
    await expect(card.getByRole("alert")).toHaveText("Add a short reason before rejecting.");
    expect(api.seen.filter((s) => s.method === "POST")).toEqual([]);

    await card.getByLabel("Review note").fill("Wrong relative.");
    await card.getByRole("button", { name: "Reject" }).click();
    await expect.poll(() => body).toEqual({ decision: "rejected", review_note: "Wrong relative." });
  });

  test("a failed review is shown, not swallowed", async ({ page, context }) => {
    await boot(page, context, "clinical_reviewer", {
      ...MODULE_HANDLERS,
      [`POST /evidence/${FACTS[0].id}/review`]: () => ({ ...errorBody("CONFLICT", "This fact was already reviewed.", 409) }),
    });
    await page.goto("/evidence-review");
    await page.getByRole("article").nth(0).getByRole("button", { name: "Accept" }).click();
    await expect(alerts(page).filter({ hasText: "The review was not saved" })).toContainText("This fact was already reviewed.");
  });

  test("observer sees the queue but every mutating action is disabled", async ({ page, context }) => {
    const { api } = await boot(page, context, "observer", MODULE_HANDLERS);
    await page.goto("/evidence-review");
    await expect(page.getByRole("article")).toHaveCount(4);
    for (const name of ["Accept", "Reject"]) {
      const buttons = page.getByRole("button", { name: new RegExp(name) });
      await expect(buttons).toHaveCount(4);
      for (let i = 0; i < 4; i++) {
        await expect(buttons.nth(i)).toHaveAttribute("aria-disabled", "true");
        await expect(buttons.nth(i)).toHaveAttribute("title", "Read-only access");
      }
      await buttons.first().click({ force: true });
    }
    await expect(page.getByLabel("Review note")).toHaveCount(0);
    expect(api.seen.filter((s) => s.method !== "GET")).toEqual([]);
  });
});

test.describe("guidelines", () => {
  test("a reviewer approves a version in review after confirming", async ({ page, context }) => {
    const { api } = await boot(page, context, "clinical_reviewer", {
      ...MODULE_HANDLERS,
      [`POST /guidelines/${GUIDELINE_ID}/versions/${VERSION_ID}/approve`]: () => ({ body: ok({}) }),
    });
    await page.goto(`/guidelines/${GUIDELINE_ID}`);
    await expect(page.getByRole("table", { name: "Guideline versions" }).getByRole("row")).toHaveCount(3);
    await page.getByRole("button", { name: "Approve version" }).click();
    expect(api.seen.filter((s) => s.method === "POST")).toEqual([]); // first click only asks to confirm
    await page.getByRole("button", { name: "Confirm approval" }).click();
    await expect.poll(() => api.seen.filter((s) => s.method === "POST").length).toBe(1);
  });

  test("population admin lacks guidelines.approve: action disabled", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto(`/guidelines/${GUIDELINE_ID}`);
    await expect(page.getByRole("button", { name: "Approve version" })).toHaveAttribute("aria-disabled", "true");
  });
});

test.describe("care gaps", () => {
  test("detail shows the rule version, status history in order and simulated outreach", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto(`/care-gaps/${CASE_ID}`);
    const history = page.getByRole("list", { name: "Status history" }).getByRole("listitem");
    await expect(history).toHaveCount(2);
    await expect(history.first()).toContainText("Created as");
    await expect(history.last()).toContainText("Awaiting capacity");
    await expect(page.getByText("Screening interval, version 2")).toBeVisible();
    await expect(page.getByText("Decided by the versioned rule above, not by an AI model.")).toBeVisible();
    await expect(page.getByRole("list", { name: "Outreach history" })).toContainText("Simulated");
  });

  test("a reviewer changes status; escalation needs a reason; the PATCH carries the bearer token", async ({ page, context }) => {
    let body: unknown = null;
    const { api } = await boot(page, context, "clinical_reviewer", {
      ...MODULE_HANDLERS,
      [`PATCH /care-gaps/${CASE_ID}/status`]: (req) => {
        body = req.postDataJSON();
        return MODULE_HANDLERS[`GET /care-gaps/${CASE_ID}`](req, new URL(req.url()));
      },
    });
    await page.goto(`/care-gaps/${CASE_ID}`);
    await page.getByLabel("New status").selectOption("escalated");
    await page.getByRole("button", { name: "Update status" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "at least 3 characters" })).toBeVisible();
    expect(api.seen.filter((s) => s.method === "PATCH")).toEqual([]);

    await page.getByLabel("New status").selectOption("planned");
    await page.getByRole("button", { name: "Update status" }).click();
    await expect.poll(() => body).toEqual({ status: "planned", reason: null });
    expect(api.seen.find((s) => s.method === "PATCH")?.headers.authorization).toMatch(/^Bearer /);
  });

  test("observer cannot change status", async ({ page, context }) => {
    const { api } = await boot(page, context, "observer", MODULE_HANDLERS);
    await page.goto(`/care-gaps/${CASE_ID}`);
    const btn = page.getByRole("button", { name: /Update status/ });
    await expect(btn).toHaveAttribute("aria-disabled", "true");
    await btn.click({ force: true });
    expect(api.seen.filter((s) => s.method !== "GET")).toEqual([]);
  });
});

test.describe("planning", () => {
  for (const [w, h] of [[1440, 900], [390, 844]] as const) {
    test(`run detail shows allocations as proposals and why cases stayed unallocated at ${w}px`, async ({ page, context }) => {
      await page.setViewportSize({ width: w, height: h });
      const { api } = await boot(page, context, "population_admin", {
        ...MODULE_HANDLERS,
        [`POST /planning/allocations/${ALLOC_ID}/confirm`]: () => ({ body: ok({ newly_confirmed: true, appointment_created: false, allocation: {} }) }),
      });
      await page.goto("/planning");
      await page.getByRole("button", { name: "Open run" }).click();
      await expect(page.getByRole("table", { name: "Unallocated cases" }).getByRole("row")).toHaveCount(3);
      await expect(page.getByRole("table", { name: "Coverage by district" }).getByRole("row")).toHaveCount(3);
      await expect(page.getByRole("table", { name: "Coverage by language" }).getByRole("row")).toHaveCount(3);
      const allocs = page.getByRole("table", { name: "Allocations" });
      await expect(allocs.getByRole("row")).toHaveCount(3);
      await expect(allocs.getByText("A proposal, not an appointment")).toHaveCount(1);
      await expect(allocs.getByText("Simulated booking")).toHaveCount(2);
      await expect(page.locator(".skeleton")).toHaveCount(0);
      await page.screenshot({ path: `test-results/screenshots/planning-run-${w}.png`, fullPage: true });

      if (w === 1440) {
        await page.getByRole("button", { name: "Confirm allocation" }).click();
        await expect.poll(() => api.seen.filter((s) => s.method === "POST").length).toBe(1);
        expect(api.seen.find((s) => s.method === "POST")?.path).toBe(`/planning/allocations/${ALLOC_ID}/confirm`);
        await expect(page.getByText("No appointment has been created.")).toBeVisible();
      }
    });
  }

  test("observer cannot confirm an allocation", async ({ page, context }) => {
    const { api } = await boot(page, context, "observer", MODULE_HANDLERS);
    await page.goto("/planning");
    await page.getByRole("button", { name: "Open run" }).click();
    const confirm = page.getByRole("button", { name: /Confirm allocation/ });
    await expect(confirm).toHaveAttribute("aria-disabled", "true");
    await confirm.click({ force: true });
    expect(api.seen.filter((s) => s.method !== "GET")).toEqual([]);
  });
});

test.describe("unavailable and failing endpoints", () => {
  test("a 404 on a collection route is an honest 'not yet available', not an error", async ({ page, context }) => {
    await boot(page, context, "population_admin", {
      "GET /care-gaps": () => ({ ...errorBody("NOT_FOUND", "The requested resource was not found.", 404) }),
    });
    await page.goto("/care-gaps");
    await expect(page.getByRole("heading", { name: "Endpoint not yet available" })).toBeVisible();
    await expect(alerts(page)).toHaveCount(0);
    await expect(page.getByText("GET /api/v1/care-gaps")).toBeVisible();
  });

  test("a 500 is shown as an error with its request id and a retry", async ({ page, context }) => {
    let fail = true;
    await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      "GET /residents": (r, u) => (fail ? { ...errorBody("INTERNAL_ERROR", "Database error.", 500) } : MODULE_HANDLERS["GET /residents"](r, u)),
    });
    await page.goto("/residents");
    await expect(alerts(page)).toContainText("Database error.");
    await expect(alerts(page)).toContainText("req-test");
    fail = false;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByRole("table", { name: "Residents" }).getByRole("row")).toHaveCount(4);
  });

  test("an empty collection explains what to do next", async ({ page, context }) => {
    await boot(page, context, "population_admin", { "GET /residents": () => ({ body: paged([], 1, 25, 0) }) });
    await page.goto("/residents");
    await expect(page.getByRole("heading", { name: "No residents have been ingested yet" })).toBeVisible();
  });
});
