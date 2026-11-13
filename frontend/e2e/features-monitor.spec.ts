import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { ok } from "./helpers";

// Live processing monitor (feature 13), route /monitor. Invented demo values for tests only.
const ADMIN = ["system.configure"];
const WIDTHS = [
  { w: 1440, h: 900 },
  { w: 390, h: 844 },
] as const;

const job = (over: Record<string, unknown> = {}) => ({
  id: "aaaaaaaa-0000-4000-8000-0000000000a1",
  job_type: "evidence.extract_batch",
  status: "running",
  total_items: 10000,
  processed_items: 4210,
  succeeded_items: 4200,
  failed_items: 10,
  percent_complete: 0.421,
  created_at: "2026-11-12T07:00:00Z",
  started_at: "2026-11-12T07:00:10Z",
  updated_at: "2026-11-12T08:00:00Z",
  completed_at: null,
  heartbeat_age_seconds: 4,
  error_summary: null,
  ...over,
});

const processing = (over: Record<string, unknown> = {}) => ({
  generated_at: "2026-11-12T08:00:30Z",
  cache_ttl_seconds: 10,
  synthetic_banner: "x",
  pipeline: {
    available: true,
    source: "synthetic",
    definition: "x",
    dataset_version: "syn-20261101-s1",
    state: "running",
    manifest_status: "running",
    resident_target: 200000,
    batch_size: 5000,
    manifest_updated_at: "2026-11-12T08:00:00Z",
    idle_seconds: 30,
    generation: { batches_planned: 40, batches_completed: 40, batches_running: 0, batches_failed: 0, residents_generated: 200000, percent: 1, throughput_residents_per_hour: null, eta_hours: 0, throughput_basis: "x" },
    ingestion: { dataset_loaded: true, dataset_status: "loading", batches_loaded: 30, residents_loaded: 150000, profiles: { full: 100000, rules: 50000 }, profiles_basis: "Residents staged per storage profile. Full is all clinical detail.", throughput_residents_per_hour: 25000, throughput_basis: "residents loaded in the last hour", eta_hours: 2, percent: 0.75 },
    eta_hours: 2,
    eta_basis: "The slower of generation and loading at their recent rates.",
  },
  jobs: { source: "live", definition: "x", by_status: { running: 1, completed: 12, failed: 2 }, failed_last_24h: 1, active: [job()], recent: [job({ id: "aaaaaaaa-0000-4000-8000-0000000000a2", job_type: "rules.evaluate_population", status: "completed", processed_items: 100, total_items: 100, succeeded_items: 100, failed_items: 0 }), job({ id: "aaaaaaaa-0000-4000-8000-0000000000a3", status: "failed", error_summary: "Batch 3 hit its retry limit - try again" })] },
  ai_spend: {
    source: "live",
    definition: "x",
    total_usd: 18.4,
    by_provider: { anthropic: 18.4 },
    models: [{ provider: "anthropic", model_id: "test-model", calls: 5200, cost_usd: 18.4, input_tokens: 4100000, output_tokens: 520000, cache_hits: 40, failed_calls: 3 }],
    invocations: 5200,
    anthropic: { spend_usd: 18.4, run_budget_usd: 25, largest_run_spend_usd: 17.5, largest_run_share_of_budget: 0.7, run_budget_basis: "The per run cap applies to one job run", spent_today_usd: 9.2, daily_budget_usd: 30, daily_share_of_budget: 0.3067 },
    nebius: { enabled: false, spend_usd: 0, credit_usd: 100, remaining_usd: 100, share_of_credit_used: 0 },
  },
  documents: { source: "synthetic", definition: "x", documents_total: 100000, documents_by_status: { extracted: 10000, registered: 90000 }, evidence_facts_total: 126000, evidence_facts_by_validation_status: { accepted: 120000, requires_review: 5000, rejected: 1000 } },
  ...over,
});

const handlers = (n = { calls: 0 }) => ({
  "GET /analytics/processing": () => {
    n.calls++;
    return { body: ok(processing()) };
  },
});

test("shows pipeline, jobs, AI spend against budget and note counts", async ({ page, context }) => {
  await bootB(page, context, "population_admin", handlers(), ADMIN);
  await page.goto("/monitor");
  await expect(page.getByRole("heading", { level: 1, name: "Live monitor" })).toBeVisible();
  const pipe = page.getByRole("region", { name: "Resident pipeline" });
  await expect(pipe).toContainText("150,000");
  await expect(pipe).toContainText("75% of 200,000");
  await expect(pipe).toContainText("30 of 40");
  await expect(pipe).toContainText("25,000 per hour");
  await expect(pipe).toContainText("2 h 00 min");
  await expect(pipe).toContainText("100,000"); // full detail
  await expect(pipe).toContainText("50,000"); // rules only
  await expect(pipe.getByText("Running", { exact: true })).toBeVisible();
  await expect(pipe.getByRole("progressbar", { name: "Residents loaded" })).toHaveAttribute("aria-valuenow", "150000");
  const jobs = page.getByRole("list", { name: "Active jobs" });
  await expect(jobs).toContainText("Evidence extract batch");
  await expect(jobs.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "4210");
  await expect(jobs).toContainText("4,210 of 10,000 items");
  const spend = page.getByRole("region", { name: "AI spend against the approved budget" });
  await expect(spend).toContainText("$18.40");
  await expect(spend).toContainText("Run cap $25.00");
  await expect(spend).toContainText("70% of the cap used");
  await expect(spend).toContainText("Daily cap $30.00");
  await expect(spend).toContainText("Nebius is not enabled");
  await expect(spend.getByRole("progressbar", { name: "Largest run spend against the run cap" })).toHaveAttribute("aria-valuenow", "17.5");
  const docs = page.getByRole("region", { name: "Notes and extracted facts" });
  await expect(docs).toContainText("100,000");
  await expect(docs).toContainText("126,000");
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expectNoDashes(page);
});

test("polls every 10 seconds and can be paused", async ({ page, context }) => {
  const n = { calls: 0 };
  await page.clock.install();
  await bootB(page, context, "population_admin", handlers(n), ADMIN);
  await page.goto("/monitor");
  await expect(page.getByRole("region", { name: "Resident pipeline" })).toBeVisible();
  const first = n.calls;
  await page.clock.fastForward(10_500);
  await expect.poll(() => n.calls).toBeGreaterThan(first);
  const second = n.calls;
  await page.getByRole("button", { name: "Pause refresh" }).click();
  await expect(page.getByText("Automatic refresh is paused.")).toBeVisible();
  await page.clock.fastForward(30_000);
  expect(n.calls).toBe(second);
  await page.getByRole("button", { name: "Resume refresh" }).click();
  await page.clock.fastForward(10_500);
  await expect.poll(() => n.calls).toBeGreaterThan(second);
});

test("says when a rate is unknown and when the pipeline manifest is missing", async ({ page, context }) => {
  let current: object = (() => {
    const p = processing();
    (p.pipeline as { eta_hours: number | null }).eta_hours = null;
    (p.pipeline.ingestion as { throughput_residents_per_hour: number | null }).throughput_residents_per_hour = null;
    return p;
  })();
  await bootB(page, context, "population_admin", { ...handlers(), "GET /analytics/processing": () => ({ body: ok(current) }) }, ADMIN);
  await page.goto("/monitor");
  const pipe = page.getByRole("region", { name: "Resident pipeline" });
  await expect(pipe.getByText("Not available").first()).toBeVisible();
  current = processing({ pipeline: { available: false, source: "synthetic", dataset_version: null, definition: "x", message: "No generation manifest found for this dataset version." } });
  await page.getByRole("button", { name: "Refresh now" }).click();
  await expect(page.getByText("No generation manifest found for this dataset version.")).toBeVisible();
});

for (const { w, h } of WIDTHS) {
  test(`monitor at ${w}px: screenshot, axe, no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: h });
    await bootB(page, context, "population_admin", handlers(), ADMIN);
    await page.goto("/monitor");
    await expect(page.getByRole("region", { name: "Notes and extracted facts" })).toBeVisible();
    await page.screenshot({ path: `test-results/screenshots/features-monitor-${w}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}

test("without system.configure the page says so and never calls the processing endpoint", async ({ page, context }) => {
  const n = { calls: 0 };
  await bootB(page, context, "observer", handlers(n));
  await page.goto("/monitor");
  await expect(page.getByText("You do not have access to this page")).toBeVisible();
  await expect(page.getByRole("region", { name: "Resident pipeline" })).toHaveCount(0);
  await page.waitForTimeout(500);
  expect(n.calls).toBe(0);
});
