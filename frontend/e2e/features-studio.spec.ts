import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { ok, paged, type ApiHandler, type TestRole } from "./helpers";

// Guideline Studio (route /guidelines/studio): read the rules as sentences, change a number, dry run it on a sample and read
// the impact against the approved version, then send it for review. Route mocked: the dry run result is an invented demo
// document with the shape of backend/anticipa/guidelines/dryrun.py. Run with: npx playwright test -c playwright.features.config.ts --workers=1
const WIDTHS = [
  { w: 1440, h: 900 },
  { w: 390, h: 844 },
] as const;

const G1 = "dddddddd-0000-4000-8000-0000000000c1";
const V2 = "dddddddd-0000-4000-8000-0000000000c2"; // approved, in force
const V3 = "dddddddd-0000-4000-8000-0000000000c3"; // not yet submitted
const V4 = "dddddddd-0000-4000-8000-0000000000c4"; // made by "Make an editable copy"
const G2 = "dddddddd-0000-4000-8000-0000000000d1";
const G2V1 = "dddddddd-0000-4000-8000-0000000000d2";
const JOB = "ffffffff-0000-4000-8000-0000000000c1";
const RULE_IDS = ["eeeeeeee-0000-4000-8000-0000000000e1", "eeeeeeee-0000-4000-8000-0000000000e2", "eeeeeeee-0000-4000-8000-0000000000e3", "eeeeeeee-0000-4000-8000-0000000000e4", "eeeeeeee-0000-4000-8000-0000000000e5"];
const RES = (n: number) => `cccccccc-0000-4000-8000-${String(n).padStart(12, "0")}`;

const SELECTORS = {
  mammogram_completed: { description: "Documented completed mammogram (screening, or unspecified indication). Symptomatic mammograms are not counted." },
  breast_cancer_personal: { description: "Resident has a documented breast cancer." },
  high_risk_lesion: { description: "Resident has a documented high-risk breast lesion on prior biopsy." },
  brca_family: { description: "Blood relative with documented breast or ovarian cancer." },
};
const OBR_INCLUSION = { true: null, false: "not_eligible", unknown: "insufficient_evidence" };
const OBR_EXCLUSION = { true: "not_eligible", false: null, unknown: "insufficient_evidence" };
const leaf = (field: string, operator: string, value: unknown) => ({ field, operator, value });

const ruleDefs = (minAge: number): Record<string, unknown>[] => [
  { rule_id: "breast.age_min", name: "Inclusion: minimum age", rule_type: "inclusion", priority: 10, expression: leaf("resident.age", "GREATER_THAN_OR_EQUAL", minAge), outcome_by_expression_result: OBR_INCLUSION, reason_code: "below_minimum_age" },
  { rule_id: "breast.age_max", name: "Inclusion: maximum age", rule_type: "inclusion", priority: 11, expression: leaf("resident.age", "LESS_THAN_OR_EQUAL", 74), outcome_by_expression_result: OBR_INCLUSION, reason_code: "above_maximum_age" },
  { rule_id: "breast.excluded", name: "Exclusion: personal history", rule_type: "exclusion", priority: 20, expression: { operator: "OR", conditions: [leaf("evidence.selector", "EXISTS", "breast_cancer_personal"), leaf("evidence.selector", "EXISTS", "high_risk_lesion")] }, outcome_by_expression_result: OBR_EXCLUSION, reason_code: "personal_history" },
  { rule_id: "breast.family", name: "Review: strong family history", rule_type: "exclusion", priority: 30, expression: { operator: "AND", conditions: [leaf("evidence.brca_family.min_age_at_onset", "LESS_THAN", 50), leaf("evidence.brca_family.distinct_subjects", "GREATER_THAN_OR_EQUAL", 2)] }, outcome_by_expression_result: { true: "requires_clinical_review", false: null, unknown: "insufficient_evidence" }, reason_code: "strong_family_history" },
  { rule_id: "breast.done", name: "Timing: screened recently", rule_type: "temporal", priority: 40, expression: { operator: "AND", conditions: [leaf("evidence.selector", "EXISTS", "mammogram_completed"), leaf("evidence.mammogram_completed.latest_event_date", "DATE_WITHIN", { years: 2 })] }, outcome_by_expression_result: { true: "already_completed", false: null, unknown: "insufficient_evidence" }, reason_code: "screening_up_to_date" },
];

const example = (n: number, change: string, outcome: string, was: string | null, district: string, band: string, reason: string) => ({
  resident_id: RES(n), change, outcome, reason_code: outcome === "eligible" ? null : "below_minimum_age", reason, baseline_outcome: was, baseline_reason_code: was ? "below_minimum_age" : null, district, age_band: band, sex: "female",
});
const EXAMPLES = [
  ...Array.from({ length: 8 }, (_, i) => example(101 + i, "newly_due", "eligible", "not_eligible", i % 2 ? "D02" : "D01", "30-39", "Eligible: a screening mammogram is indicated and none is documented in the last 2 years.")),
  ...Array.from({ length: 4 }, (_, i) => example(201 + i, "no_longer_due", "not_eligible", "eligible", "D03", "70-79", "Outside the screening age range.")),
  ...Array.from({ length: 8 }, (_, i) => example(301 + i, "unchanged", i % 2 ? "eligible" : "already_completed", i % 2 ? "eligible" : "already_completed", "D04", "50-59", "No change from the approved version.")),
];

const resultDoc = (hash: string, random = false) => ({
  persisted: false,
  engine_version: "guideline-engine/1",
  guideline: { id: G1, key: "breast_cancer_screening", name: "Breast cancer screening" },
  version: { id: V3, version: 3, review_status: "draft", content_hash: hash, care_activity: "breast_cancer_screening" },
  baseline: { id: V2, version: 2, review_status: "approved", content_hash: "approved-hash", care_activity: "breast_cancer_screening" },
  evaluation_date: "2026-11-13",
  sample: random
    ? { type: "random_n", requested: 2500, evaluated: 2500, cohort_id: null, district: "D01", seed: 1, population_residents: 21480 }
    : { type: "ai_cohort", requested: null, evaluated: 10000, cohort_id: "ai-test-cohort", district: null, seed: 1, population_residents: 10000 },
  runtime_seconds: 94.2,
  residents_per_second: 106.1,
  residents_with_errors: 0,
  outcomes: { eligible: 3120, not_eligible: 4800, already_completed: 1400, insufficient_evidence: 520, requires_clinical_review: 160 },
  baseline_outcomes: { eligible: 1880, not_eligible: 6040, already_completed: 1400, insufficient_evidence: 520, requires_clinical_review: 160 },
  by_district: { D01: { eligible: 900, not_eligible: 500 }, D02: { eligible: 700, not_eligible: 600 } },
  by_age_band: { "30-39": { eligible: 900 }, "40-49": { eligible: 1100 } },
  by_sex: { female: { eligible: 3120, not_eligible: 4800 } },
  district_names: { D01: { name: "Bhopal", state: "Madhya Pradesh" }, D02: { name: "Indore", state: "Madhya Pradesh" }, D03: { name: "Jabalpur", state: "Madhya Pradesh" }, D04: { name: "Gwalior", state: "Madhya Pradesh" }, D05: { name: "Pune", state: "Maharashtra" } },
  diff: {
    eligible_baseline: 1880, eligible_version: 3120, newly_due: 1240, no_longer_due: 0, changed_other: 0, unchanged: 8760,
    newly_due_by_district: { D01: 560, D02: 430, D03: 90, D04: 80, D05: 80 },
    newly_due_by_age_band: { "30-39": 900, "40-49": 340 }, newly_due_by_sex: { female: 1240 }, no_longer_due_by_district: {},
    transitions: [{ from: "not_eligible", to: "eligible", count: 1240 }],
  },
  top_reason_codes: [
    { reason_code: "below_minimum_age", outcome: "not_eligible", count: 3200, baseline_count: 4400 },
    { reason_code: "screening_up_to_date", outcome: "already_completed", count: 1400, baseline_count: 1400 },
  ],
  examples: EXAMPLES,
  estimate: random
    ? { basis: "A seeded random sample of 2,500 of 21,480 residents. Estimate with a 95 percent interval.", population_residents: 21480, eligible: { estimate: 6700, low: 6400, high: 7000 }, newly_due: { estimate: 2660, low: 2450, high: 2870 }, no_longer_due: { estimate: 0, low: 0, high: 0 } }
    : null,
  notes: ["Nothing was saved. No evaluation, care gap case or outreach was created or changed.", "The version under test runs on the same deterministic engine as production. No AI model is called."],
});

const version = (id: string, g: string, n: number, status: string, over: Record<string, unknown> = {}) => ({
  id, guideline_id: g, version: n, review_status: status, source_reference: "Example Task Force, 2024", source_publication_date: "2024-04-30", effective_from: "2026-10-20", effective_until: null,
  population_scope: { description: "Women aged 40 to 74" }, disclaimer: "A preventive care opportunity, not a diagnosis.", change_summary: null, care_activity: "breast_cancer_screening", priority: "high", source_version: null,
  clinical_review_status: status === "approved" ? "clinically_reviewed" : "pending_clinical_review", content_hash: `hash-${id}`, created_at: "2026-11-02T09:00:00Z", submitted_at: null, approved_by: null, approved_at: status === "approved" ? "2026-10-20T09:00:00Z" : null, retired_at: null, retirement_reason: null, ...over,
});

/** A mutable mock backend for the Studio routes. `calls` records every request body so tests can assert what was sent. */
function studio(opts: { withRun?: boolean } = {}) {
  const st = { minAge: 40, hash: "hash-1", status: "draft", job: opts.withRun ? { polls: 99 } : (null as null | { polls: number }), random: false, hasV4: false, rules: ruleDefs(40), calls: [] as { key: string; body: unknown }[] };
  const rec = (key: string, req: { postDataJSON(): unknown }) => st.calls.push({ key, body: (() => { try { return req.postDataJSON(); } catch { return null; } })() });
  const ruleOut = (def: Record<string, unknown>, i: number) => ({
    id: RULE_IDS[i], guideline_version_id: V3, rule_key: String(def.rule_id).split(".")[1], rule_id: def.rule_id, rule_type: def.rule_type, precedence: def.priority, version: 1, name: def.name, description: null, care_activity: "breast_cancer_screening",
    priority: "high", rule_language: "anticipa-08-v1", content_hash: "x", approval_status: st.status, created_at: "2026-11-02T09:00:00Z", rule_definition: def,
  });
  const full = (id: string, n: number, status: string) => ({ ...version(id, G1, n, status, { content_hash: id === V3 ? st.hash : `hash-${id}` }), selectors: SELECTORS, decision_policy: { rank: ["not_eligible"] }, sources: [], version_metadata: { eligible_explanation: "Eligible." }, rules: st.rules.map(ruleOut) });
  const versions = () => [version(V3, G1, 3, st.status), ...(st.hasV4 ? [version(V4, G1, 4, "draft")] : []), version(V2, G1, 2, "approved")].sort((a, b) => b.version - a.version);
  const jobOut = (done: boolean) => ({
    id: JOB, version_id: V3, status: done ? "completed" : "running", scope: {}, total_items: 10000, processed_items: done ? 10000 : 4000, failed_items: 0, error_summary: null,
    created_at: "2026-11-13T09:00:00Z", started_at: "2026-11-13T09:00:01Z", completed_at: done ? "2026-11-13T09:01:35Z" : null, result: done ? resultDoc(st.hash, st.random) : null,
  });
  const handlers: Record<string, ApiHandler> = {
    "GET /guidelines": () => ({ body: paged([
      { id: G1, guideline_key: "breast_cancer_screening", name: "Breast cancer screening", description: null, topic: "cancer", authoritative_source: "Example Task Force", source_url: null, created_at: "2026-10-01T09:00:00Z", updated_at: "2026-11-02T09:00:00Z", versions: versions() },
      { id: G2, guideline_key: "colorectal_screening", name: "Colorectal cancer screening", description: null, topic: "cancer", authoritative_source: "Example Task Force", source_url: null, created_at: "2026-10-01T09:00:00Z", updated_at: "2026-11-02T09:00:00Z", versions: [version(G2V1, G2, 1, "approved")] },
    ]) }),
    [`GET /guidelines/${G1}/versions/${V3}`]: () => ({ body: ok(full(V3, 3, st.status)) }),
    [`GET /guidelines/${G1}/versions/${V2}`]: () => ({ body: ok(full(V2, 2, "approved")) }),
    [`GET /guidelines/${G1}/versions/${V4}`]: () => ({ body: ok(full(V4, 4, "draft")) }),
    [`GET /guidelines/${G2}/versions/${G2V1}`]: () => ({ body: ok({ ...full(G2V1, 1, "approved"), guideline_id: G2, rules: [] }) }),
    "POST /rules/validate": (req) => {
      rec("validate", req);
      const rules = (req.postDataJSON() as { rules: { expression: { value?: unknown } }[] }).rules;
      const bad = rules.some((r) => r.expression?.value === 95);
      return { body: ok(bad ? { valid: false, errors: [{ field: "rules.breast.age_min.expression.value", reason: "minimum age must be below the maximum age" }] } : { valid: true, errors: [], rules: {} }) };
    },
    [`DELETE /rules/${RULE_IDS[0]}`]: (req) => (rec("delete", req), { body: ok({ id: RULE_IDS[0], deleted: true }) }),
    "POST /rules": (req) => {
      rec("add", req);
      const def = (req.postDataJSON() as { definition: { rule_id: string; expression: { value: number } } }).definition;
      st.rules = st.rules.map((r) => (r.rule_id === def.rule_id ? def : r));
      st.minAge = def.expression.value;
      st.hash = `hash-${st.minAge}`;
      return { status: 201, body: ok(ruleOut(def, 0)) };
    },
    [`POST /guidelines/${G1}/versions/${V3}/dry-run`]: (req) => {
      rec("dry-run", req);
      st.random = (req.postDataJSON() as { sample: string }).sample === "random_n";
      st.job = { polls: 0 };
      return { status: 202, body: ok(jobOut(false)) };
    },
    [`GET /guidelines/${G1}/versions/${V3}/dry-runs`]: () => ({ body: ok(st.job ? [jobOut(st.job.polls >= 3)] : []) }),
    [`GET /guidelines/${G1}/versions/${V3}/dry-runs/${JOB}`]: () => ({ body: ok(jobOut(st.job ? st.job.polls++ >= 3 : false)) }),
    [`GET /guidelines/${G1}/versions/${V2}/dry-runs`]: () => ({ body: ok([]) }),
    [`GET /guidelines/${G1}/versions/${V4}/dry-runs`]: () => ({ body: ok([]) }),
    [`GET /guidelines/${G2}/versions/${G2V1}/dry-runs`]: () => ({ body: ok([]) }),
    [`POST /guidelines/${G1}/versions/${V3}/submit-review`]: (req) => (rec("submit", req), (st.status = "in_review"), { body: ok(full(V3, 3, "in_review")) }),
    [`POST /guidelines/${G1}/versions`]: (req) => (rec("copy", req), (st.hasV4 = true), { status: 201, body: ok(full(V4, 4, "draft")) }),
    "GET /analytics/geography": (_r, url) => ({
      body: ok(url.searchParams.get("level") === "state"
        ? { level: "state", areas: [{ level: "district", code: "D01", name: "Bhopal", state_code: "23", state: "Madhya Pradesh" }, { level: "district", code: "D02", name: "Indore", state_code: "23", state: "Madhya Pradesh" }] }
        : { level: "country", areas: [{ level: "state", code: "23", name: "Madhya Pradesh", coverage_status: "pilot" }] }),
    }),
  };
  return { st, handlers };
}

const open = async (page: Page, role: TestRole, s = studio(), extra: Record<string, ApiHandler> = {}) => {
  await bootB(page, page.context(), role, { ...s.handlers, ...extra });
  await page.goto("/guidelines/studio");
  await expect(page.getByRole("heading", { level: 1, name: "Guideline Studio" })).toBeVisible();
  return s;
};
const rule = (page: Page, name: string) => page.getByRole("article", { name: `Rule: ${name}` });
const callsOf = (s: ReturnType<typeof studio>, key: string) => s.st.calls.filter((c) => c.key === key);

test.describe("guideline studio", () => {
  test("shows the version not yet submitted, with its rules written as sentences", async ({ page }) => {
    await open(page, "population_admin");
    await expect(page.getByLabel("Version", { exact: true })).toHaveValue(V3);
    await expect(page.getByRole("heading", { name: "Rules of version 3" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Rules of version 3" }).getByText("Not yet submitted")).toBeVisible();
    const minAge = rule(page, "Inclusion: minimum age");
    await expect(minAge).toContainText("Age is at least");
    await expect(minAge.getByRole("spinbutton", { name: /Age is at least/ })).toHaveValue("40");
    await expect(minAge).toContainText("When they are not met");
    await expect(minAge).toContainText("Not eligible");
    // group wording, selector wording (first sentence of the description, joining hyphens removed) and a look back window
    const excluded = rule(page, "Exclusion: personal history");
    await expect(excluded).toContainText("Any one of these");
    await expect(excluded).toContainText("Evidence found: Resident has a documented breast cancer");
    await expect(excluded).toContainText("Evidence found: Resident has a documented high risk breast lesion on prior biopsy");
    await expect(rule(page, "Timing: screened recently").getByRole("spinbutton", { name: /latest event within the last/ })).toHaveValue("2");
    await expect(rule(page, "Review: strong family history").getByRole("spinbutton")).toHaveCount(2);
    await expectNoDashes(page);
  });

  test("an invalid number shows the API's validation feedback and saves nothing", async ({ page }) => {
    const s = await open(page, "population_admin");
    const card = rule(page, "Inclusion: minimum age");
    const input = card.getByRole("spinbutton", { name: /Age is at least/ });
    await input.fill("200");
    await expect(card.getByText("Enter a whole number from 0 to 130")).toBeVisible();
    await expect(card.getByRole("button", { name: "Check and save change" })).toBeDisabled();
    await input.fill("95");
    await card.getByRole("button", { name: "Check and save change" }).click();
    const alert = card.getByRole("alert");
    await expect(alert).toContainText("The change is not valid, so it was not saved");
    await expect(alert).toContainText("minimum age must be below the maximum age");
    expect(callsOf(s, "validate")).toHaveLength(1);
    expect(callsOf(s, "delete")).toHaveLength(0); // nothing was removed or added
    expect(callsOf(s, "add")).toHaveLength(0);
    await card.getByRole("button", { name: "Discard change" }).click();
    await expect(input).toHaveValue("40");
  });

  test("a valid change is validated first, then replaces the rule", async ({ page }) => {
    const s = await open(page, "population_admin");
    const card = rule(page, "Inclusion: minimum age");
    await card.getByRole("spinbutton", { name: /Age is at least/ }).fill("30");
    await expect(card.getByText("was 40")).toBeVisible();
    await card.getByRole("button", { name: "Check and save change" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Saved." })).toContainText("now uses the new numbers");
    expect(s.st.calls.map((c) => c.key)).toEqual(["validate", "delete", "add"]);
    const sent = callsOf(s, "add")[0].body as { guideline_version_id: string; definition: { rule_id: string; expression: { value: number } } };
    expect(sent.guideline_version_id).toBe(V3);
    expect(sent.definition.rule_id).toBe("breast.age_min");
    expect(sent.definition.expression.value).toBe(30);
    await expect(rule(page, "Inclusion: minimum age").getByRole("spinbutton", { name: /Age is at least/ })).toHaveValue("30");
  });

  test("dry run: progress, then the impact against the approved version, with examples that open a journey", async ({ page }) => {
    const s = await open(page, "population_admin");
    await expect(page.getByText("No dry run has been run for this version yet")).toBeVisible();
    await page.getByRole("button", { name: "Run dry run" }).click();
    const body = callsOf(s, "dry-run")[0].body as Record<string, unknown>;
    expect(body.sample).toBe("ai_cohort");
    expect(body.district).toBeUndefined();
    const progress = page.getByRole("progressbar", { name: "Dry run progress" });
    await expect(progress).toBeVisible();
    await expect(page.getByText("Nothing is saved while this runs")).toBeVisible();

    const results = page.getByTestId("dry-run-results");
    await expect(results).toBeVisible();
    await expect(results.getByRole("heading", { level: 3 })).toHaveText("+1,240 residents newly due, mainly Bhopal and Indore");
    await expect(results.getByText("Preview, nothing saved")).toBeVisible();
    await expect(results).toContainText("Version 3 against approved version 2");
    await expect(results).toContainText("the AI cohort (10,000 residents)");
    await expect(results).toContainText("not scaled to the whole population");
    const totals = page.getByRole("region", { name: "Dry run totals" });
    await expect(totals).toContainText("1,880");
    await expect(totals).toContainText("3,120");
    await expect(totals).toContainText("1,240");
    const outcomes = results.getByRole("list", { name: "Residents by outcome" });
    await expect(outcomes).toContainText("Not eligible");
    await expect(outcomes).toContainText("was 6,040");
    await expect(results.getByRole("list", { name: "Newly due residents by district" })).toContainText("Bhopal");
    await expect(results.getByRole("list", { name: "Newly due residents by age band" })).toContainText("30 to 39");
    await expect(results.getByRole("table", { name: /Reasons residents are not due/ })).toContainText("Below minimum age");
    const examples = results.getByRole("list", { name: "Example residents" });
    await expect(examples.getByRole("listitem")).toHaveCount(20);
    await expect(examples.getByRole("link").first()).toHaveAttribute("href", `/residents/${RES(101)}/journey`);
    await expect(examples).toContainText("Newly due");
    await expect(examples).toContainText("was not eligible");
    await expect(results.getByText("Out of date")).toHaveCount(0);
    await expectNoDashes(page);
  });

  test("a random sample of one district sends its choices and reports a labelled estimate", async ({ page }) => {
    const s = await open(page, "clinical_reviewer");
    await page.getByLabel("Sample", { exact: true }).selectOption("random_n");
    await page.getByLabel("Sample size").selectOption("2500");
    await page.getByLabel("District", { exact: true }).selectOption({ label: "Bhopal" });
    await page.getByRole("button", { name: "Run dry run" }).click();
    expect(callsOf(s, "dry-run")[0].body).toMatchObject({ sample: "random_n", n: 2500, district: "D01" });
    const results = page.getByTestId("dry-run-results");
    await expect(results).toContainText("About 2,660 residents newly due across the 21,480 in scope (95 percent interval 2,450 to 2,870).");
    await expect(results).toContainText("a random sample of 2,500 of 21,480 residents in Bhopal");
    await expect(results).not.toContainText("not scaled to the whole population");
  });

  test("changing a rule after a run marks the result as out of date", async ({ page }) => {
    await open(page, "population_admin", studio({ withRun: true }));
    const results = page.getByTestId("dry-run-results");
    await expect(results).toBeVisible();
    await expect(results.getByText("Out of date")).toHaveCount(0);
    const card = rule(page, "Inclusion: minimum age");
    await card.getByRole("spinbutton", { name: /Age is at least/ }).fill("35");
    await card.getByRole("button", { name: "Check and save change" }).click();
    await expect(page.getByTestId("dry-run-results").getByText("Out of date: the rules changed after this run")).toBeVisible();
  });

  test("an observer reads rules and an earlier result, and cannot edit, run or submit", async ({ page }) => {
    await open(page, "observer", studio({ withRun: true }));
    await expect(page.getByText("Read only access. You can read every rule")).toBeVisible();
    const card = rule(page, "Inclusion: minimum age");
    await expect(card.getByRole("spinbutton")).toHaveCount(0);
    await expect(card.locator("strong")).toHaveText("40");
    await expect(page.getByRole("button", { name: "Run dry run" })).toHaveAttribute("aria-disabled", "true");
    await expect(page.getByRole("button", { name: "Submit for review" })).toHaveAttribute("aria-disabled", "true");
    await expect(page.getByTestId("dry-run-results").getByRole("heading", { level: 3 })).toHaveText("+1,240 residents newly due, mainly Bhopal and Indore");
  });

  test("an approved version is locked, and an editable copy carries its rules", async ({ page }) => {
    const s = await open(page, "population_admin");
    await page.getByLabel("Version", { exact: true }).selectOption(V2);
    await expect(page.getByRole("heading", { name: "Rules of version 2" })).toBeVisible();
    await expect(page.getByText("Locked", { exact: true })).toBeVisible();
    await expect(page.getByText("This version is approved and in force. Its rules are locked.")).toBeVisible();
    await expect(rule(page, "Inclusion: minimum age").getByRole("spinbutton")).toHaveCount(0);
    await page.getByRole("button", { name: "Make an editable copy" }).click();
    const sent = callsOf(s, "copy")[0].body as { rules: unknown[]; selectors: object; care_activity: string; metadata: object; change_summary: string };
    expect(sent.rules).toHaveLength(5);
    expect(Object.keys(sent.selectors)).toContain("mammogram_completed");
    expect(sent.care_activity).toBe("breast_cancer_screening");
    expect(sent.metadata).toEqual({ eligible_explanation: "Eligible." });
    expect(sent.change_summary).toContain("Working copy of version 2");
    await expect(page.getByRole("heading", { name: "Rules of version 4" })).toBeVisible();
    await expect(page.getByLabel("Version", { exact: true })).toHaveValue(V4);
  });

  test("submit for review sends the version and then shows it waiting for a reviewer", async ({ page }) => {
    const s = await open(page, "population_admin");
    await page.getByRole("button", { name: "Submit for review" }).click();
    expect(callsOf(s, "submit")).toHaveLength(1);
    await expect(page.getByText("This version is waiting for a clinical reviewer. Its rules are locked.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open the guideline to review or approve it" })).toHaveAttribute("href", `/guidelines/${G1}`);
  });

  for (const { w, h } of WIDTHS) {
    test(`${w}px: screenshot, axe, no horizontal scroll, no dashes`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await open(page, "population_admin", studio({ withRun: true }));
      await expect(page.getByTestId("dry-run-results")).toBeVisible();
      await page.screenshot({ path: `test-results/screenshots/features-studio-${w}.png`, fullPage: true });
      expect(await page.evaluate(() => document.documentElement.scrollWidth), "page must not scroll horizontally").toBeLessThanOrEqual(w);
      await expectNoDashes(page);
      const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
      expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
    });
  }
});
