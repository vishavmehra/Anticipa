import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { CASE_ID, GUIDELINE_ID, MODULE_HANDLERS, RES_ID } from "./fixtures";
import { job, meFor, mockApi, mockSupabase, ok, paged, signIn } from "./helpers";

// Screenshots at desktop and phone widths, plus an axe WCAG 2.x A/AA scan and a no-horizontal-scroll check.
const WIDTHS = [
  { w: 1440, h: 900 },
  { w: 390, h: 844 },
] as const;
const JOB_ID = "aaaaaaaa-0000-4000-8000-000000000001";

const jobs = [
  job({ id: JOB_ID, status: "running", processed_items: 4210, total_items: 10000, succeeded_items: 4200, failed_items: 10, completed_at: null }),
  job({ id: "aaaaaaaa-0000-4000-8000-000000000002", job_type: "evidence.extract_batch", status: "failed", processed_items: 50, total_items: 100, failed_items: 7, error_summary: "Batch 3 exceeded its retry budget." }),
  job({ id: "aaaaaaaa-0000-4000-8000-000000000003", job_type: "rules.evaluate_population", status: "partially_completed", processed_items: 99, total_items: 100, failed_items: 1 }),
  job({ id: "aaaaaaaa-0000-4000-8000-000000000004", status: "queued", processed_items: 0, succeeded_items: 0, started_at: null, completed_at: null }),
  job({ id: "aaaaaaaa-0000-4000-8000-000000000005", status: "cancelled", processed_items: 12 }),
  job({ id: "aaaaaaaa-0000-4000-8000-000000000006" }),
];
const audit = [
  { id: "bbbbbbbb-0000-4000-8000-000000000001", occurred_at: "2026-11-05T09:14:31Z", actor_id: "11111111-1111-4111-8111-111111111111", actor_role: "population_admin", action: "job.retry", resource_type: "processing_job", resource_id: JOB_ID, outcome: "success", request_id: "req-1a2b3c4d", details: { job_type: "evidence.extract_batch", previous_status: "failed" } },
  { id: "bbbbbbbb-0000-4000-8000-000000000002", occurred_at: "2026-11-05T09:10:02Z", actor_id: null, actor_role: null, action: "job.complete", resource_type: "processing_job", resource_id: JOB_ID, outcome: "failure", request_id: null, details: {} },
  { id: "bbbbbbbb-0000-4000-8000-000000000003", occurred_at: "2026-11-05T08:59:40Z", actor_id: "11111111-1111-4111-8111-111111111111", actor_role: "care_coordinator", action: "job.cancel", resource_type: "processing_job", resource_id: JOB_ID, outcome: "denied", request_id: "req-9f8e7d6c", details: { reason: "missing processing.execute" } },
];
const integrations = [
  { key: "postgres", name: "PostgreSQL + pgvector", configured: true, status: "healthy", detail: "Connected; pgvector 0.8.0.", latency_ms: 4.2, checked_at: "2026-11-05T09:14:31Z" },
  { key: "hapi", name: "HAPI FHIR R4", configured: true, status: "degraded", detail: "Unexpected FHIR version 5.0.0 (expected R4 4.0.x).", latency_ms: 120.4, checked_at: "2026-11-05T09:14:31Z" },
  { key: "claude", name: "Anthropic Claude", configured: true, status: "authentication_failed", detail: "Provider rejected the key (HTTP 401).", latency_ms: null, checked_at: "2026-11-05T09:14:31Z" },
  { key: "nebius", name: "Nebius Token Factory", configured: false, status: "not_configured", detail: "API key is not set.", latency_ms: null, checked_at: "2026-11-05T09:14:31Z" },
];

interface Screen {
  name: string;
  path: string;
  signedIn: boolean;
  /** Role to sign in as. Defaults to population_admin; system pages need system_admin. */
  role?: "population_admin" | "system_admin";
  ready: (p: Page) => Locator;
}
const SCREENS: Screen[] = [
  { name: "login", path: "/login", signedIn: false, ready: (p) => p.getByRole("heading", { name: "Sign in to Anticipa" }) },
  { name: "command-center", path: "/", signedIn: true, ready: (p) => p.getByRole("list", { name: "Open care gaps by priority" }) },
  { name: "population", path: "/population", signedIn: true, ready: (p) => p.getByRole("heading", { level: 1 }) },
  { name: "insights", path: "/insights", signedIn: true, ready: (p) => p.getByRole("heading", { level: 1 }) },
  { name: "reports", path: "/reports", signedIn: true, ready: (p) => p.getByRole("heading", { level: 1 }) },
  { name: "residents", path: "/residents", signedIn: true, ready: (p) => p.getByRole("list", { name: "Residents", exact: true }).getByRole("listitem").nth(2) },
  { name: "resident-detail", path: `/residents/${RES_ID}`, signedIn: true, ready: (p) => p.getByRole("article").nth(3) },
  { name: "evidence-review", path: "/evidence-review", signedIn: true, ready: (p) => p.getByRole("article").first() },
  { name: "guidelines", path: "/guidelines", signedIn: true, ready: (p) => p.getByRole("table", { name: "Guidelines" }).getByRole("row").nth(3) },
  { name: "guideline-detail", path: `/guidelines/${GUIDELINE_ID}`, signedIn: true, ready: (p) => p.getByRole("table", { name: "Rules" }).getByRole("row").nth(1) },
  { name: "care-gaps", path: "/care-gaps", signedIn: true, ready: (p) => p.getByRole("table", { name: "Care gap cases" }).getByRole("row").nth(2) },
  { name: "care-gap-detail", path: `/care-gaps/${CASE_ID}`, signedIn: true, ready: (p) => p.getByRole("table", { name: "Completion evidence" }).getByRole("row").nth(1) },
  { name: "planning", path: "/planning", signedIn: true, ready: (p) => p.getByRole("table", { name: "Planning runs" }).getByRole("row").nth(1) },
  { name: "engagement", path: "/engagement", signedIn: true, ready: (p) => p.getByRole("table", { name: "Appointments", exact: true }).getByRole("row").nth(1) },
  { name: "evaluation", path: "/evaluation", signedIn: true, ready: (p) => p.getByRole("table", { name: "Metrics" }).getByRole("row").nth(2) },
  { name: "jobs", path: "/jobs", signedIn: true, role: "system_admin", ready: (p) => p.getByRole("table", { name: "Background jobs" }).getByRole("row").nth(6) },
  { name: "job-detail", path: `/jobs/${JOB_ID}`, signedIn: true, role: "system_admin", ready: (p) => p.getByRole("list", { name: "Job lifecycle" }) },
  { name: "audit", path: "/audit", signedIn: true, role: "system_admin", ready: (p) => p.getByRole("table", { name: "Audit events" }).getByRole("row").nth(3) },
  { name: "integrations", path: "/integrations", signedIn: true, role: "system_admin", ready: (p) => p.getByRole("table", { name: "Integration health" }).getByRole("row").nth(4) },
  { name: "primitives", path: "/dev/primitives", signedIn: false, ready: (p) => p.getByRole("heading", { name: "Primitives" }) },
];

for (const { w, h } of WIDTHS) {
  test.describe(`at ${w}px`, () => {
    test.use({ viewport: { width: w, height: h } });

    for (const s of SCREENS) {
      test(`${s.name}: screenshot, axe, no horizontal scroll`, async ({ page, context }) => {
        if (s.signedIn) {
          const role = s.role ?? "population_admin";
          await mockSupabase(page, { role });
          await signIn(context, role);
          await mockApi(page, {
            ...MODULE_HANDLERS,
            "GET /auth/me": () => ({ body: ok(meFor(s.role ?? "population_admin")) }),
            "GET /jobs": () => ({ body: paged(jobs, 1, 25, 4081) }),
            [`GET /jobs/${JOB_ID}`]: () => ({ body: ok(jobs[0]) }),
            [`GET /jobs/${JOB_ID}/progress`]: () => ({
              body: ok({ job_id: JOB_ID, status: "running", total_items: 10000, processed_items: 4210, succeeded_items: 4200, failed_items: 10, percent_complete: 42.1, item_status_counts: {}, heartbeat_at: null, updated_at: "2026-11-05T09:10:00Z" }),
            }),
            "GET /audit": () => ({ body: paged(audit, 1, 50, 3) }),
            "GET /integrations": () => ({ body: ok(integrations) }),
          });
        }
        await page.goto(s.path);
        await expect(s.ready(page)).toBeVisible();
        if (s.name !== "primitives") await expect(page.locator(".skeleton")).toHaveCount(0); // every block has finished loading (the primitives page shows skeletons on purpose)
        await expect(page.getByRole("note")).toHaveCount(0); // owner decision: no synthetic banner

        await page.screenshot({ path: `test-results/screenshots/${s.name}-${w}.png`, fullPage: true });

        const scrollW = await page.evaluate(() => document.documentElement.scrollWidth);
        const wide = scrollW > w ? await page.evaluate((limit) => [...document.querySelectorAll("body *")].filter((e) => e.getBoundingClientRect().right > limit + 0.5).slice(0, 6).map((e) => `${e.tagName}.${String((e as HTMLElement).className).slice(0, 60)}@${Math.round(e.getBoundingClientRect().right)}`).join(" | "), w) : "";
        expect(scrollW, `page must not scroll horizontally. Wide: ${wide}`).toBeLessThanOrEqual(w);

        const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
        expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
      });
    }
  });
}

test.describe("phone navigation", () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test("menu button reveals the primary navigation", async ({ page, context }) => {
    await mockSupabase(page, { role: "population_admin" });
    await signIn(context, "population_admin");
    await mockApi(page, { "GET /auth/me": () => ({ body: ok(meFor("population_admin")) }) });
    await page.goto("/");
    const menu = page.getByRole("button", { name: "Menu", exact: true });
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("link", { name: "Residents" })).toBeHidden();
    await menu.click();
    await expect(page.getByRole("button", { name: "Close menu" })).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("link", { name: "Residents" })).toBeVisible();
    await page.screenshot({ path: "test-results/screenshots/menu-open-390.png", fullPage: true });
  });
});
