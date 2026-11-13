import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import {
  alerts,
  errorBody,
  job,
  meFor,
  mockApi,
  mockSupabase,
  ok,
  paged,
  sessionFor,
  signIn,
  type ApiHandler,
  type TestRole,
} from "./helpers";

const JOB_ID = "aaaaaaaa-0000-4000-8000-000000000001";
const FAILED_ID = "aaaaaaaa-0000-4000-8000-000000000002";
const tokenOf = (marker: string) => `Bearer ${sessionFor("population_admin", marker).access_token}`;

const progress = (over: Record<string, unknown> = {}) => ({
  job_id: JOB_ID,
  status: "running",
  total_items: 100,
  processed_items: 40,
  succeeded_items: 40,
  failed_items: 0,
  percent_complete: 40,
  item_status_counts: {},
  heartbeat_at: null,
  updated_at: "2026-11-05T09:10:00Z",
  ...over,
});

/** Signed-in browser with mocked Supabase + API. Extra handlers override the defaults. */
async function boot(page: Page, context: BrowserContext, role: TestRole, handlers: Record<string, ApiHandler> = {}) {
  const supabase = await mockSupabase(page, { role });
  await signIn(context, role);
  // The shell calls these two on every page (bell badge and district scope pill).
  const shell = {
    "GET /analytics/alerts/summary": () => ({ body: ok({ alerts_active: 0, by_severity: {}, items: [] }) }),
    "GET /meta/scope": () => ({ body: ok({ country: { code: "IN", name: "India" }, states: [], districts: [] }) }),
  };
  const api = await mockApi(page, { "GET /auth/me": () => ({ body: ok(meFor(role)) }), ...shell, ...handlers });
  return { supabase, api };
}

const jobRows = [
  job({ id: JOB_ID, status: "running", processed_items: 40, succeeded_items: 40 }),
  job({ id: FAILED_ID, job_type: "evidence.extract_batch", status: "failed", processed_items: 50, succeeded_items: 43, failed_items: 7, error_summary: "Batch 3 exceeded its retry budget." }),
  job({ id: "aaaaaaaa-0000-4000-8000-000000000003", job_type: "rules.evaluate_population", status: "completed" }),
];

test.describe("auth and shell", () => {
  test("login renders with the synthetic banner and no sign-up", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Sign in to Anticipa" })).toBeVisible();
    await expect(page.getByLabel("Email")).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
    await expect(page.getByRole("note")).toHaveCount(0); // owner decision: no synthetic banner
    await expect(page.getByText(/sign up|create account|register/i)).toHaveCount(0);
  });

  test("password sign-in goes through Supabase and lands in the shell", async ({ page }) => {
    const { calls } = await mockSupabase(page, { role: "population_admin" });
    await mockApi(page, { "GET /auth/me": () => ({ body: ok(meFor("population_admin")) }) });
    await page.goto("/login");
    await page.getByLabel("Email").fill("admin@example.test");
    await page.getByLabel("Password").fill("correct horse");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Residents" })).toBeVisible();
    expect(calls.passwordBodies).toEqual([{ email: "admin@example.test", password: "correct horse", gotrue_meta_security: {} }]);
  });

  test("wrong credentials show the Supabase message", async ({ page }) => {
    await mockSupabase(page, { passwordOk: false });
    await page.goto("/login");
    await page.getByLabel("Email").fill("x@example.test");
    await page.getByLabel("Password").fill("nope");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(alerts(page)).toContainText("Invalid login credentials");
  });

  test("a visit without a session is redirected to /login with next", async ({ page }) => {
    await page.goto("/jobs");
    await expect(page).toHaveURL(/\/login\?next=%2Fjobs$/);
  });

  test("403 from /auth/me says no Anticipa role is assigned", async ({ page, context }) => {
    await mockSupabase(page);
    await signIn(context, "population_admin");
    await mockApi(page, { "GET /auth/me": () => ({ ...errorBody("FORBIDDEN", "No role.", 403) }) });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Access pending. Ask an administrator." })).toBeVisible();
    await expect(page.getByRole("note")).toHaveCount(0); // owner decision: no synthetic banner
  });

  test("navigation follows permissions from /auth/me, not the role name", async ({ page, context }) => {
    await boot(page, context, "care_coordinator");
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Primary" });
    await expect(nav.getByRole("link", { name: "Command Center" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Care gaps" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Jobs" })).toHaveCount(0);
    await expect(nav.getByRole("link", { name: "Integrations" })).toHaveCount(0); // system pages are not product navigation
    await expect(nav.getByRole("link", { name: "Evidence review" })).toHaveCount(0);
    await expect(page.getByRole("note")).toHaveCount(0); // owner decision: no synthetic banner
  });

  test("modules without an endpoint show an honest empty state", async ({ page, context }) => {
    await boot(page, context, "population_admin");
    await page.goto("/residents");
    await expect(page.getByRole("heading", { name: "Endpoint not yet available" })).toBeVisible();
    await expect(page.getByText(/\d+ residents/)).toHaveCount(0);
  });

  test("keyboard: skip link is first and focus is visible", async ({ page, context }) => {
    await boot(page, context, "population_admin");
    await page.goto("/");
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to content" });
    await expect(skip).toBeFocused();
    await expect(skip).toBeVisible();
    await expect(skip).toHaveCSS("outline-style", "solid");
  });
});

test.describe("jobs", () => {
  test("table renders mocked rows, bearer token, no CSRF header", async ({ page, context }) => {
    const { api } = await boot(page, context, "system_admin", {
      "GET /jobs": () => ({ body: paged(jobRows, 1, 25, 3) }),
    });
    await page.goto("/jobs");
    const table = page.getByRole("table", { name: "Background jobs" });
    await expect(table.getByRole("row")).toHaveCount(4);
    await expect(table.locator("[data-status=running]")).toBeVisible();
    await expect(table.locator("[data-status=failed]")).toBeVisible();
    await expect(table.locator("[data-status=completed]")).toBeVisible();
    await expect(page.getByText("Showing 1 to 3 of 3")).toBeVisible();
    const call = api.seen.find((s) => s.path === "/jobs");
    expect(call?.headers.authorization).toBe(tokenOf("access-1"));
    expect(call?.headers["x-anticipa-csrf"]).toBeUndefined();
    expect(api.missed).toEqual([]);
  });

  test("status filter sends the status query", async ({ page, context }) => {
    const statuses: (string | null)[] = [];
    await boot(page, context, "system_admin", {
      "GET /jobs": (_r, url) => {
        statuses.push(url.searchParams.get("status"));
        return { body: paged(jobRows, 1, 25, 3) };
      },
    });
    await page.goto("/jobs");
    await expect(page.getByRole("table", { name: "Background jobs" })).toBeVisible();
    await page.getByLabel("Status").selectOption("failed");
    await expect.poll(() => statuses.at(-1)).toBe("failed");
  });

  test("empty and error states", async ({ page, context }) => {
    let fail = true;
    await boot(page, context, "system_admin", {
      "GET /jobs": () => (fail ? { ...errorBody("INTERNAL_ERROR", "Database error.", 500) } : { body: paged([], 1, 25, 0) }),
    });
    await page.goto("/jobs");
    await expect(alerts(page)).toContainText("Database error.");
    await expect(alerts(page)).toContainText("req-test");
    fail = false;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByRole("heading", { name: "No jobs have run yet" })).toBeVisible();
  });

  test("detail polls progress and settles on the final state", async ({ page, context }) => {
    let polls = 0;
    await boot(page, context, "system_admin", {
      [`GET /jobs/${JOB_ID}`]: () => ({ body: ok(polls >= 2 ? job({ id: JOB_ID, status: "completed" }) : job({ id: JOB_ID, status: "running", processed_items: 20, succeeded_items: 20, completed_at: null })) }),
      [`GET /jobs/${JOB_ID}/progress`]: () => {
        polls++;
        return { body: ok(polls === 1 ? progress() : progress({ status: "completed", processed_items: 100, succeeded_items: 100, percent_complete: 100 })) };
      },
    });
    await page.goto(`/jobs/${JOB_ID}`);
    const bar = page.getByRole("progressbar", { name: "Job progress" });
    await expect(bar).toHaveAttribute("aria-valuenow", "20");
    await expect(bar).toHaveAttribute("aria-valuenow", "40");
    await expect(page.locator("[data-status=completed]").first()).toBeVisible();
    await expect(bar).toHaveAttribute("aria-valuenow", "100");
    await expect(page.getByRole("list", { name: "Job lifecycle" })).toBeVisible();
  });

  test("admin can retry a failed job (POST with bearer)", async ({ page, context }) => {
    const { api } = await boot(page, context, "system_admin", {
      [`GET /jobs/${FAILED_ID}`]: () => ({ body: ok(jobRows[1]) }),
      [`POST /jobs/${FAILED_ID}/retry`]: () => ({ body: ok(job({ id: FAILED_ID, status: "queued" })) }),
    });
    await page.goto(`/jobs/${FAILED_ID}`);
    const retry = page.getByRole("button", { name: /Retry job/ });
    await expect(retry).toBeEnabled();
    await retry.click();
    await expect(page.getByText("Retry requested.")).toBeVisible();
    const post = api.seen.find((s) => s.method === "POST");
    expect(post?.path).toBe(`/jobs/${FAILED_ID}/retry`);
    expect(post?.headers.authorization).toBe(tokenOf("access-1"));
  });

  test("the observer role is read only: product pages only, job controls absent", async ({ page, context }) => {
    const { api } = await boot(page, context, "observer", {
      [`GET /jobs/${FAILED_ID}`]: () => ({ body: ok(jobRows[1]) }),
    });
    await page.goto(`/jobs/${FAILED_ID}`);
    // system pages are closed to everyone without system.configure, and nothing is fetched for them
    await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Retry job/ })).toHaveCount(0);
    const nav = page.getByRole("navigation", { name: "Primary" });
    for (const label of ["Command Center", "Population", "Residents", "Care gaps", "Evidence review", "Guidelines", "Capacity planning", "Outreach", "Evaluation"]) {
      await expect(nav.getByRole("link", { name: label })).toBeVisible();
    }
    for (const label of ["Jobs", "Audit", "Integrations", "Live monitor"]) await expect(nav.getByRole("link", { name: label })).toHaveCount(0);
    await expect(page.getByText("System", { exact: true })).toHaveCount(0);
    expect(api.seen.filter((x) => x.path.startsWith("/jobs"))).toEqual([]);
    expect(api.seen.filter((x) => x.method !== "GET")).toEqual([]);
  });

  test("population admin does not see the System section and system pages are closed to them", async ({ page, context }) => {
    const { api } = await boot(page, context, "population_admin");
    await page.goto("/");
    await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Command Center" })).toBeVisible();
    await expect(page.getByText("System", { exact: true })).toHaveCount(0);
    for (const path of ["/jobs", "/audit", "/integrations", "/monitor"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: "You do not have access to this page" })).toBeVisible();
    }
    expect(api.seen.filter((x) => /^\/(jobs|audit|integrations)/.test(x.path))).toEqual([]);
  });

  test("system admin finds Jobs, Audit, Integrations and Live monitor under one System entry", async ({ page, context }) => {
    await boot(page, context, "system_admin", { "GET /jobs": () => ({ body: paged(jobRows, 1, 25, 3) }) });
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Primary" });
    await expect(nav.getByRole("link", { name: "Jobs" })).toHaveCount(0); // main navigation stays product only
    await page.getByText("System", { exact: true }).click();
    for (const label of ["Live monitor", "Jobs", "Audit", "Integrations"]) await expect(page.getByRole("link", { name: label, exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Jobs", exact: true }).click();
    await expect(page.getByRole("table", { name: "Background jobs" }).getByRole("row")).toHaveCount(4);
  });
});

test.describe("session refresh", () => {
  test("a 401 refreshes the session once and retries with the new token", async ({ page, context }) => {
    const { supabase, api } = await boot(page, context, "system_admin", {
      "GET /jobs": (req) =>
        req.headers().authorization === tokenOf("access-1")
          ? { ...errorBody("UNAUTHENTICATED", "Token expired.", 401) }
          : { body: paged(jobRows, 1, 25, 3) },
    });
    await page.goto("/jobs");
    await expect(page.getByRole("table", { name: "Background jobs" }).getByRole("row")).toHaveCount(4);
    expect(supabase.calls.refreshes).toBe(1);
    const jobCalls = api.seen.filter((s) => s.path === "/jobs");
    expect(jobCalls.map((c) => c.headers.authorization)).toEqual([tokenOf("access-1"), tokenOf("access-2")]);
  });

  test("a failed refresh sends the user to /login", async ({ page, context }) => {
    await mockSupabase(page, { refresh: "fail" });
    await signIn(context, "system_admin");
    await mockApi(page, {
      "GET /auth/me": () => ({ body: ok(meFor("system_admin")) }),
      "GET /jobs": () => ({ ...errorBody("UNAUTHENTICATED", "Token expired.", 401) }),
    });
    await page.goto("/jobs");
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole("heading", { name: "Sign in to Anticipa" })).toBeVisible();
  });
});

test.describe("evidence quote primitive", () => {
  test("renders English, Arabic (rtl) and Devanagari highlights", async ({ page }) => {
    await page.goto("/dev/primitives");
    const en = page.locator("[data-sample=en]");
    await expect(en.locator("mark")).toHaveText("mother diagnosed with breast cancer at age 46");
    await expect(en.locator("figcaption")).toContainText("Matches the stored quote");

    const ar = page.locator("[data-sample=ar]");
    await expect(ar.locator("blockquote")).toHaveAttribute("dir", "rtl");
    await expect(ar.locator("blockquote")).toHaveCSS("direction", "rtl");
    await expect(ar.locator("mark")).toHaveText("أصيبت الأم بسرطان الثدي في عمر 46 سنة");

    const hi = page.locator("[data-sample=hi]");
    await expect(hi.locator("mark")).toHaveText("माँ को 46 वर्ष की आयु में स्तन कैंसर का निदान हुआ था");
    await expect(hi.locator("blockquote")).toHaveAttribute("lang", "hi");

    await expect(page.locator("[data-sample=mismatch] figcaption")).toContainText("Offsets do not match the stored quote");
    await expect(page.locator("[data-sample=out-of-range]")).toContainText("outside this document");
  });
});

test.describe("google sign-in", () => {
  test("the login page offers Continue with Google next to email and password", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("button", { name: "Continue with Google" })).toBeVisible();
    await expect(page.getByLabel("Email")).toBeVisible();
    await expect(page.getByText("Judges: continue with Google for read only access.")).toBeVisible();
    await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  });

  test("Continue with Google starts the OAuth flow with the callback as redirect", async ({ page }) => {
    await mockSupabase(page);
    await page.goto("/login");
    const request = page.waitForRequest((r) => r.url().includes("/auth/v1/authorize"));
    await page.getByRole("button", { name: "Continue with Google" }).click();
    const url = new URL((await request).url());
    expect(url.searchParams.get("provider")).toBe("google");
    expect(url.searchParams.get("redirect_to")).toBe("http://localhost:3100/auth/callback");
  });

  test("the callback without a code returns to /login with a clear message", async ({ page }) => {
    await page.goto("/auth/callback");
    await expect(page).toHaveURL(/\/login\?error=oauth$/);
    await expect(alerts(page)).toContainText("Google sign-in did not complete");
  });

  test("a provider error on the callback returns to /login", async ({ page }) => {
    await page.goto("/auth/callback?error=access_denied&error_description=denied");
    await expect(page).toHaveURL(/\/login\?error=oauth$/);
  });
});
