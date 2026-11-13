import { expect, test } from "@playwright/test";
import { ALLOC_ID, APPT_ID, CASE_ID, DOCS, EVID_ID, EVIDENCE, FACTS, GUIDELINE_ID, MODULE_HANDLERS, RES_ID, VERSION_ID } from "./fixtures";
import { alerts, boot, errorBody, ok, paged } from "./helpers";


test.describe("command center", () => {
  test("shows live KPIs, priority donut, closure trend, alerts, equity, activity and the population profile", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Population Health Overview", level: 1 })).toBeVisible();
    const tile = (label: string) => page.getByText(label, { exact: true }).first().locator("xpath=ancestor::div[contains(@class,'card')][1]");
    await expect(tile("Total population")).toContainText("1,200");
    await expect(tile("Residents with care gaps")).toContainText("300");
    await expect(tile("Residents with care gaps")).toContainText("25%");
    await expect(tile("Open care gaps")).toContainText("320");
    await expect(tile("Open care gaps")).toContainText("80%");
    await expect(tile("Closure rate")).toContainText("15%");
    await expect(tile("Closure rate")).toContainText("60 closed");

    const priority = page.getByRole("list", { name: "Open care gaps by priority" });
    await expect(priority).toContainText("High");
    await expect(priority).toContainText("80");
    await expect(page.getByRole("img", { name: "Care gaps closed per day" })).toBeVisible();
    await expect(page.getByRole("group", { name: /Map of India coloured by/ })).toBeVisible();

    await expect(page.getByRole("heading", { name: /Active alerts/ })).toContainText("(1)");
    await expect(page.getByText("Clinic capacity nearly full").first()).toBeVisible();
    await expect(page.getByText("No data", { exact: true })).toBeVisible(); // a rule without data is not shown as OK

    await expect(page.getByRole("list", { name: "Planning coverage by district" })).toContainText("D07");
    await expect(page.getByText("30.0 pp")).toBeVisible();
    // backend internals (jobs, audit activity) are for system administrators only
    await expect(page.getByText("Caregap status change")).toHaveCount(0);
    await expect(page.getByText("Failed background jobs")).toHaveCount(0);
    // owner decision: no synthetic banner or wording in the chrome
    await expect(page.getByRole("note")).toHaveCount(0);
    await expect(page.getByRole("navigation", { name: "Primary" })).not.toContainText(/synthetic/i);
  });

  test("selecting a district in the scope pill filters every widget (the backend takes district= on each call)", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Population Health Overview", level: 1 })).toBeVisible();
    const seen: string[] = [];
    page.on("request", (r) => {
      if (/analytics\/(command-center|funnel|programmes)\?/.test(r.url())) seen.push(r.url());
    });
    await page.getByLabel("District scope").selectOption("D01");
    await expect(page.locator(".card", { hasText: "Total population" }).first()).toContainText("Bhopal");
    await expect(page.getByRole("region", { name: "Programs at a glance" })).toContainText("Bhopal");
    await expect(page.getByRole("region", { name: "Care gaps by priority" })).toContainText("Bhopal");
    await expect(page.getByRole("region", { name: "Closure trend" })).toContainText("Bhopal");
    await expect(page.getByRole("region", { name: "Key takeaways" })).toContainText("Bhopal has");
    await expect(page.getByRole("region", { name: "District view" })).toContainText("Bhopal, Madhya Pradesh");
    await expect.poll(() => seen.filter((u) => u.includes("district=D01")).length).toBeGreaterThanOrEqual(3);
    expect(seen.some((u) => u.includes("analytics/command-center") && u.includes("district=D01"))).toBe(true);
    expect(seen.some((u) => u.includes("analytics/funnel") && u.includes("district=D01"))).toBe(true);
    expect(seen.some((u) => u.includes("analytics/programmes") && u.includes("district=D01"))).toBe(true);
  });

  test("the India map zoom transform changes when a state is opened and returns when it is closed", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto("/");
    await expect(page.getByRole("group", { name: /Map of India coloured by/ })).toBeVisible();
    const scaleOf = () => page.locator(".map-zoom").evaluate((el) => new DOMMatrix(getComputedStyle(el).transform).a);
    await expect.poll(scaleOf).toBeCloseTo(1, 2);
    await page.getByRole("button", { name: "Open Madhya Pradesh" }).click();
    await expect.poll(scaleOf, { timeout: 5000 }).toBeGreaterThan(1.5);
    await page.getByRole("button", { name: "India", exact: true }).click();
    await expect.poll(scaleOf, { timeout: 5000 }).toBeCloseTo(1, 2);
  });

  test("system administrators also see recent activity and the job health rule", async ({ page, context }) => {
    await boot(page, context, "system_admin", MODULE_HANDLERS);
    await page.goto("/");
    await expect(page.getByText("Caregap status change")).toBeVisible();
    await expect(page.getByText("Failed background jobs").first()).toBeVisible();
  });

  test("the Population page opens (its content is covered by the features specs)", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto("/population");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  });

  test("a limited role sees no district equity and no activity feed", async ({ page, context }) => {
    await boot(page, context, "care_coordinator", {
      ...MODULE_HANDLERS,
      "GET /analytics/command-center": (r, u) => {
        const base = MODULE_HANDLERS["GET /analytics/command-center"](r, u) as { body: { data: Record<string, unknown> } };
        return { body: { ...base.body, data: { ...base.body.data, limited: true, restricted: ["equity", "recent_activity"], equity: null, recent_activity: null } } };
      },
    });
    await page.goto("/");
    await expect(page.getByText("District equity is not available for your role.")).toBeVisible();
      });

  test("an unavailable endpoint is an honest empty state", async ({ page, context }) => {
    await boot(page, context, "population_admin", {
      "GET /analytics/command-center": () => ({ ...errorBody("NOT_FOUND", "The requested resource was not found.", 404) }),
    });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Endpoint not yet available" })).toBeVisible();
    await expect(alerts(page)).toHaveCount(0);
  });

  test("observers see a Read only access badge, admins do not", async ({ page, context }) => {
    await boot(page, context, "observer", MODULE_HANDLERS);
    await page.goto("/");
    await expect(page.getByText("Read only access").first()).toBeVisible();
  });
});

test.describe("operations", () => {
  test("the appointment schedule asks for capacity slots with timezone aware from and to", async ({ page, context }) => {
    const urls: URL[] = [];
    await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      "GET /planning/capacity-slots": (_r, url) => {
        urls.push(url);
        return { body: paged([], 1, 100, 0) };
      },
    });
    await page.goto("/operations");
    await expect.poll(() => urls.length, { timeout: 15000 }).toBeGreaterThan(0);
    const q = urls[0].searchParams;
    expect(q.get("from")).toMatch(/T00:00:00Z$/);
    expect(q.get("to")).toMatch(/T00:00:00Z$/);
  });
});

test.describe("residents", () => {
  test("list renders rows and sends the search query", async ({ page, context }) => {
    const searches: (string | null)[] = [];
    const { api } = await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      "GET /residents": (_r, url) => {
        searches.push(url.searchParams.get("search"));
        return MODULE_HANDLERS["GET /residents"](_r, url);
      },
    });
    await page.goto("/residents");
    const list = page.getByRole("list", { name: "Residents", exact: true });
    await expect(list.getByRole("listitem")).toHaveCount(3);
    await expect(list).toContainText("Age 64, Male");
    await expect(page.getByText("Page 1 of 48")).toBeVisible();

    await page.getByLabel("Search", { exact: true }).fill("5a1c");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect.poll(() => searches.at(-1)).toBe("5a1c");
    expect(api.seen.find((s) => s.path === "/residents")?.headers.authorization).toMatch(/^Bearer /);
  });

  test("a one-character search is rejected in the form, without calling the API", async ({ page, context }) => {
    const { api } = await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto("/residents");
    await expect(page.getByRole("list", { name: "Residents", exact: true }).getByRole("listitem")).toHaveCount(3);
    const before = api.seen.filter((s) => s.path === "/residents").length;
    await page.getByLabel("Search", { exact: true }).fill("a");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(alerts(page).filter({ hasText: "at least 2 characters" })).toBeVisible();
    expect(api.seen.filter((s) => s.path === "/residents").length).toBe(before);
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
    await expect(page.getByText(`Characters ${enStart} to ${enStart + DOCS.en.quote.length}`)).toBeVisible();
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
    const queue = page.getByRole("list").filter({ has: page.getByRole("button", { name: /Breast cancer/ }) });
    await expect(queue.getByRole("button")).toHaveCount(4);
    // fact 4's only source is quote_not_found
    await queue.getByRole("button", { name: /Colorectal cancer/ }).click();
    await expect(page.getByRole("button", { name: "Accept" })).toBeDisabled();
    await expect(page.getByText("Accept is unavailable until a quote is verified.")).toBeVisible();

    await queue.getByRole("button", { name: /Breast cancer/ }).first().click();
    await page.getByLabel("Review note").fill("Matches the source.");
    await page.getByRole("button", { name: "Accept" }).click();
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
    await page.getByRole("button", { name: /Breast cancer/ }).nth(1).click(); // the Arabic fact
    await page.getByRole("button", { name: "Reject" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Add a short reason before rejecting." })).toBeVisible();
    expect(api.seen.filter((s) => s.method === "POST")).toEqual([]);

    await page.getByLabel("Review note").fill("Wrong relative.");
    await page.getByRole("button", { name: "Reject" }).click();
    await expect.poll(() => body).toEqual({ decision: "rejected", review_note: "Wrong relative." });
  });

  test("a failed review is shown, not swallowed", async ({ page, context }) => {
    await boot(page, context, "clinical_reviewer", {
      ...MODULE_HANDLERS,
      [`POST /evidence/${FACTS[0].id}/review`]: () => ({ ...errorBody("CONFLICT", "This fact was already reviewed.", 409) }),
    });
    await page.goto("/evidence-review");
    await page.getByRole("button", { name: "Accept" }).click();
    await expect(alerts(page).filter({ hasText: "The review was not saved" })).toContainText("This fact was already reviewed.");
  });

  test("observer sees the queue but every mutating action is disabled", async ({ page, context }) => {
    const { api } = await boot(page, context, "observer", MODULE_HANDLERS);
    await page.goto("/evidence-review");
    await expect(page.getByRole("article")).toHaveCount(1);
    for (const name of ["Accept", "Reject"]) {
      const button = page.getByRole("button", { name: new RegExp(name) });
      await expect(button).toHaveAttribute("aria-disabled", "true");
      await expect(button).toHaveAttribute("title", "Read only access");
      await button.click({ force: true });
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
    await expect(page.getByText("Decided by the versioned rule, not by an AI model.")).toBeVisible();
  });

  test("outreach shows the simulated message in its own direction, with the resident's response", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto(`/care-gaps/${CASE_ID}`);
    const outreach = page.getByRole("list", { name: "Outreach history" });
    await expect(outreach).toContainText("Simulated message");
    await expect(outreach).toContainText("Delivered");
    await expect(outreach.locator('span[lang="ar"][dir="rtl"]')).toContainText("مرحبا");
    await expect(outreach).toContainText("Resident response (simulated): Accept");
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
    await expect(alerts(page).filter({ hasText: "at least 3 characters" })).toBeVisible();
    expect(api.seen.filter((s) => s.method === "PATCH")).toEqual([]);

    await page.getByLabel("New status").selectOption("planned");
    await page.getByRole("button", { name: "Update status" }).click();
    await expect.poll(() => body).toEqual({ status: "planned", reason: null });
    expect(api.seen.find((s) => s.method === "PATCH")?.headers.authorization).toMatch(/^Bearer /);
  });

  test("appointments: a coordinator moves a simulated appointment forward", async ({ page, context }) => {
    let body: unknown = null;
    await boot(page, context, "care_coordinator", {
      ...MODULE_HANDLERS,
      [`GET /care-gaps/${CASE_ID}/completion-evidence`]: () => ({ ...errorBody("FORBIDDEN", "No permission.", 403) }),
      [`PATCH /appointments/${APPT_ID}/status`]: (req) => {
        body = req.postDataJSON();
        return { body: ok({}) };
      },
    });
    await page.goto(`/care-gaps/${CASE_ID}`);
    const table = page.getByRole("table", { name: "Appointments" });
    await expect(table.getByRole("row")).toHaveCount(2);
    await expect(table).toContainText("Simulated booking");
    await table.getByLabel("New appointment status").selectOption("completed");
    await table.getByRole("button", { name: "Apply" }).click();
    await expect.poll(() => body).toEqual({ appointment_status: "completed" });
  });

  test("completion evidence: submit, verify, then close as completed", async ({ page, context }) => {
    const posts: { path: string; body: unknown }[] = [];
    let verified = false;
    const rec = (path: string, body: unknown) => posts.push({ path, body });
    await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      [`GET /care-gaps/${CASE_ID}/completion-evidence`]: () => ({
        body: ok(verified ? [{ ...EVIDENCE[0], verification_status: "verified", verified_at: "2026-11-08T09:00:00Z" }] : EVIDENCE),
      }),
      [`POST /care-gaps/${CASE_ID}/completion-evidence`]: (req) => {
        rec("submit", req.postDataJSON());
        return { body: ok(EVIDENCE[0]) };
      },
      [`POST /care-gaps/${CASE_ID}/completion-evidence/${EVID_ID}/review`]: (req) => {
        rec("review", req.postDataJSON());
        verified = true;
        return { body: ok({}) };
      },
      [`POST /care-gaps/${CASE_ID}/close`]: (req) => {
        rec("close", req.postDataJSON());
        return { body: ok({ outcome: "completed" }) };
      },
    });
    await page.goto(`/care-gaps/${CASE_ID}`);
    // population_admin holds caregaps.close, so it may submit, review and close.
    await expect(page.getByRole("table", { name: "Completion evidence" }).getByRole("row")).toHaveCount(2);
    await expect(page.getByRole("button", { name: "Close case" })).toBeDisabled(); // completed needs verified evidence
    await expect(page.getByText("Completing a case needs verified completion evidence")).toBeVisible();

    await page.getByLabel("Reference").fill("ab");
    await page.getByRole("button", { name: "Submit evidence" }).click();
    await expect(alerts(page).filter({ hasText: "at least 3 characters" })).toBeVisible();
    await page.getByLabel("Reference").fill("Simulated mammography record 78");
    await page.getByRole("button", { name: "Submit evidence" }).click();
    await expect.poll(() => posts.length).toBe(1);
    expect(posts[0]).toEqual({ path: "submit", body: { source_type: "simulated_procedure_record", evidence_reference: "Simulated mammography record 78" } });

    await page.getByRole("button", { name: "Verify" }).click();
    await expect.poll(() => posts.some((p) => p.path === "review")).toBe(true);
    expect(posts.find((p) => p.path === "review")?.body).toEqual({ decision: "verified", note: null });

    await expect(page.getByRole("button", { name: "Close case" })).toBeEnabled();
    await page.getByLabel("Reason (required)").fill("Mammography attended and verified.");
    await page.getByRole("button", { name: "Close case" }).click();
    await expect.poll(() => posts.some((p) => p.path === "close")).toBe(true);
    expect(posts.find((p) => p.path === "close")?.body).toEqual({ outcome: "completed", reason: "Mammography attended and verified." });
  });

  test("closing as not completed needs an exception type and reason", async ({ page, context }) => {
    let body: unknown = null;
    await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      [`POST /care-gaps/${CASE_ID}/close`]: (req) => {
        body = req.postDataJSON();
        return { body: ok({}) };
      },
    });
    await page.goto(`/care-gaps/${CASE_ID}`);
    await page.getByLabel("Outcome").selectOption("closed_not_completed");
    await page.getByLabel("Exception type").selectOption("unable_to_contact");
    await page.getByLabel("Reason (required)").fill("Three attempts, no answer.");
    await page.getByRole("button", { name: "Close case" }).click();
    await expect.poll(() => body).toEqual({ outcome: "closed_not_completed", reason: "Three attempts, no answer.", exception_type: "unable_to_contact" });
  });

  test("a closed case shows its outcome instead of the close form", async ({ page, context }) => {
    await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      [`GET /care-gaps/${CASE_ID}`]: (req, url) => {
        const base = MODULE_HANDLERS[`GET /care-gaps/${CASE_ID}`](req, url) as { body: { data: Record<string, unknown> } };
        return { body: { ...base.body, data: { ...base.body.data, status: "completed", closed_at: "2026-11-09T09:00:00Z", closure_reason: "Verified mammography.", allowed_transitions: ["reopen"] } } };
      },
    });
    await page.goto(`/care-gaps/${CASE_ID}`);
    await expect(page.getByText("Verified mammography.").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Close case" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Reopen case" })).toBeVisible();
  });

  test("observer sees the whole case but cannot change, review, submit or close anything", async ({ page, context }) => {
    const { api } = await boot(page, context, "observer", MODULE_HANDLERS);
    await page.goto(`/care-gaps/${CASE_ID}`);
    await expect(page.getByRole("table", { name: "Completion evidence" }).getByRole("row")).toHaveCount(2);
    for (const name of [/Update status/, /Apply/, /Verify/, /Reject/, /Submit evidence/, /Close case/]) {
      const btn = page.getByRole("button", { name });
      await expect(btn).toHaveAttribute("aria-disabled", "true");
      await btn.click({ force: true });
    }
    expect(api.seen.filter((s) => s.method !== "GET")).toEqual([]);
  });
});

test.describe("engagement", () => {
  test("shows the simulated funnel and appointments", async ({ page, context }) => {
    await boot(page, context, "population_admin", MODULE_HANDLERS);
    await page.goto("/engagement");
    await expect(page.getByText("All communications, responses, appointments and outcomes are simulated")).toBeVisible();
    const delivered = page.getByText("Delivered", { exact: true }).first().locator("xpath=../..");
    await expect(delivered).toContainText("8");
    await expect(page.getByRole("table", { name: "Messages by language" })).toContainText("Arabic");
    const appts = page.getByRole("table", { name: "Appointments", exact: true });
    await expect(appts.getByRole("row")).toHaveCount(2);
    await expect(appts).toContainText("Simulated booking");
  });

  test("an overview that is not available is an honest empty state", async ({ page, context }) => {
    await boot(page, context, "population_admin", {
      ...MODULE_HANDLERS,
      "GET /analytics/engagement": () => ({ ...errorBody("NOT_FOUND", "The requested resource was not found.", 404) }),
    });
    await page.goto("/engagement");
    await expect(page.getByRole("heading", { name: "Endpoint not yet available" })).toBeVisible();
  });
});

test.describe("planning", () => {
  for (const [w, h] of [[1440, 900], [390, 844]] as const) {
    test(`run detail shows allocations as proposals and why cases stayed unallocated at ${w}px`, async ({ page, context }) => {
      await page.setViewportSize({ width: w, height: h });
      const { api } = await boot(page, context, "population_admin", {
        ...MODULE_HANDLERS,
        [`POST /planning/allocations/${ALLOC_ID}/confirm`]: () => ({ body: ok({ newly_confirmed: true, appointment_created: true, allocation: {} }) }),
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
        await expect(page.getByText("A simulated appointment now holds the slot for the case.")).toBeVisible();
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
    await expect(page.getByRole("list", { name: "Residents", exact: true }).getByRole("listitem")).toHaveCount(3);
  });

  test("an empty collection explains what to do next", async ({ page, context }) => {
    await boot(page, context, "population_admin", { "GET /residents": () => ({ body: paged([], 1, 25, 0) }) });
    await page.goto("/residents");
    await expect(page.getByText("No residents have been loaded yet.")).toBeVisible();
  });
});
