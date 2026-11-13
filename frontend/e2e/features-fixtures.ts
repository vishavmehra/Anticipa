import type { BrowserContext, Page } from "@playwright/test";
import { COOKIE_NAME, meFor, mockApi, mockSupabase, ok, sessionFor, type ApiHandler, type TestRole } from "./helpers";
import { DOCS } from "./fixtures";
import { conversationHandlers } from "./conversation-fixtures";

// Route-mocked data for the judge-facing feature screens (journey, explainer, note viewer, funnel, evaluation, monitor,
// pedigree, outreach preview). Invented demo values for tests only: they are not patient data and not metrics.
// Shapes mirror backend/anticipa/analytics/{routes,journey,service}.py and evaluation/{routes,report}.py (read 2026-11-12).

export const PORT_B = 3200;
export const R_ID = "cccccccc-0000-4000-8000-0000000000b1";
export const CASE_B = "eeeeeeee-0000-4000-8000-0000000000b1";
export const EVAL_ID = "dddddddd-0000-4000-8000-0000000000b1";
const GUIDELINE_B = "dddddddd-0000-4000-8000-0000000000b2";

/** Sign-in cookie for the :3200 server (helpers.signIn is pinned to :3100). */
export async function signInB(context: BrowserContext, role: TestRole) {
  const value = `base64-${Buffer.from(JSON.stringify(sessionFor(role))).toString("base64url")}`;
  await context.addCookies([{ name: COOKIE_NAME, value, url: `http://localhost:${PORT_B}` }]);
}

/** `extraPermissions` adds grants the shared role tables do not list (for example system.configure for the admin only monitor). */
export async function bootB(page: Page, context: BrowserContext, role: TestRole, handlers: Record<string, ApiHandler>, extraPermissions: string[] = []) {
  await mockSupabase(page, { role });
  await signInB(context, role);
  const me = meFor(role);
  return mockApi(page, { "GET /auth/me": () => ({ body: ok({ ...me, permissions: [...me.permissions, ...extraPermissions] }) }), ...handlers });
}

/**
 * Owner rule: no hyphen or dash in UI text. Checks the rendered text of the page content, ignoring identifiers
 * (monospace), quoted note text and resident language text, which are data and stay exactly as stored.
 */
export async function expectNoDashes(page: Page) {
  const bad = await page.evaluate(() => {
    const root = document.querySelector("#main")!.cloneNode(true) as HTMLElement;
    root.querySelectorAll(".font-mono, code, blockquote, mark, [data-note-scroll], [lang=ar], [lang=hi], option, svg").forEach((e) => e.remove());
    const text = root.textContent ?? "";
    return [...text.matchAll(/[\u2013\u2014]|(^|\s)-(\s|$)|[A-Za-z]-[A-Za-z]/g)].map((m) => text.slice(Math.max(0, (m.index ?? 0) - 30), (m.index ?? 0) + 30));
  });
  if (bad.length) throw new Error(`UI text contains dashes or hyphens: ${JSON.stringify(bad.slice(0, 5))}`);
}

/** Code-point offsets of `quote` in `text` (the unit the backend uses). */
function span(text: string, quote: string) {
  const i = text.indexOf(quote);
  const start = Array.from(text.slice(0, i)).length;
  return { start, end: start + Array.from(quote).length };
}

type Lang = keyof typeof DOCS;
const FACT_IDS: Record<Lang, string> = { en: "77777777-0000-4000-8000-0000000000e1", ar: "77777777-0000-4000-8000-0000000000e2", hi: "77777777-0000-4000-8000-0000000000e3" };

const source = (l: Lang, over: Record<string, unknown> = {}) => {
  const d = DOCS[l];
  const { start, end } = span(d.text, d.quote);
  return { id: `88888888-0000-4000-8000-0000000000e${l === "en" ? 1 : l === "ar" ? 2 : 3}`, document_id: d.id, quote: d.quote, start_offset: start, end_offset: end, verification_status: "verified_exact", location_ambiguous: false, source_reference: null, ...over };
};

export const factFor = (l: Lang, over: Record<string, unknown> = {}) => ({
  id: FACT_IDS[l],
  resident_id: R_ID,
  category: "family_history",
  fact_type: "breast_cancer",
  value: { original_text: DOCS[l].quote },
  subject_type: "family_member",
  relationship_type: "mother",
  assertion_status: "positive",
  temporal_status: "historical",
  age_at_onset: 46,
  extraction_confidence: "high",
  uncertainty_note: null,
  validation_status: "accepted",
  review_status: "pending",
  review_reasons: [],
  extraction: { method: "llm", provider: "anthropic", model_id: "test-model", prompt_version: "evidence.extract.v3" },
  extracted_at: "2026-11-11T10:00:00Z",
  synthetic_status: "synthetic",
  sources: [source(l)],
  ...over,
});

/** GET /evidence/{id}?context=N for one of the three documents. */
export function evidenceDetail(l: Lang, ctx = true, over: Record<string, unknown> = {}) {
  const d = DOCS[l];
  const { start, end } = span(d.text, d.quote);
  const cps = Array.from(d.text);
  return {
    ...factFor(l, over),
    model_provider: "anthropic",
    model_id: "test-model",
    prompt_version: "evidence.extract.v3",
    invocation_id: null,
    extraction_method: "llm",
    sources: [source(l)],
    document: { id: d.id, document_type: "progress_note", language: d.language, created_on: "2026-10-14" },
    invocation: { task: "evidence.extract", provider: "anthropic", model_id: "test-model", prompt_version: "evidence.extract.v3", input_tokens: 812, output_tokens: 96, cache_read_tokens: 0, cost_usd: 0.0021, latency_ms: 812, validation_status: "accepted", fallback_used: false, cache_hit: false, created_at: "2026-11-11T10:00:00Z" },
    review: null,
    context: ctx ? [{ source_id: source(l).id, before: cps.slice(0, start).join(""), quote: cps.slice(start, end).join(""), after: cps.slice(end).join("") }] : [],
  };
}

const HI_OVER = { fact_type: "colorectal_cancer", relationship_type: "father", assertion_status: "negative", temporal_status: "unknown" };

const criteria = [
  { criterion_id: "age_50_74", rule_type: "inclusion", priority: 1, result: true, outcome: "passed", detail: "Resident is 56 years old on the evaluation date (inclusion range 50 to 74)." },
  { criterion_id: "no_mammogram_in_24_months", rule_type: "inclusion", priority: 2, result: true, outcome: "passed", detail: "No mammogram is recorded in the 24 months before the evaluation date." },
  { criterion_id: "no_bilateral_mastectomy", rule_type: "exclusion", priority: 3, result: false, outcome: "passed", detail: "No bilateral mastectomy is recorded." },
  { criterion_id: "family_history_pathway", rule_type: "inclusion", priority: 4, result: "unknown", outcome: "unknown", detail: "Mother's cancer onset age is stated in two notes with conflicting statements.", reason_code: "conflicting_information", review_required: true },
  { criterion_id: "recent_screening_elsewhere", rule_type: "exclusion", priority: 5, result: "unknown", outcome: "unknown", detail: "No outside screening record is on file to confirm or rule out a recent mammogram.", reason_code: "missing_data" },
  { criterion_id: "age_over_74", rule_type: "exclusion", priority: 6, result: true, outcome: "failed", detail: "Resident is not older than 74." },
];

export const journeyEval = (over: Record<string, unknown> = {}) => ({
  id: EVAL_ID,
  is_latest: true,
  outcome: "requires_clinical_review",
  outcome_class: "indeterminate",
  reason_code: "conflicting_information",
  review_status: "pending_clinical_review",
  explanation: { engine_version: "1.0", evaluation_date: "2026-11-11", decision: { outcome: "requires_clinical_review", reason_code: "conflicting_information", decisive_rule_id: "family_history_pathway", basis: "decision_step_2", explanation: "Records disagree on a relative's cancer history, so a clinician must review." }, criteria },
  evidence_references: [
    { type: "evidence_fact", id: FACT_IDS.en, document_id: DOCS.en.id, quote: DOCS.en.quote },
    { type: "clinical_record", id: "rec-1", source_reference: "Observation/mammogram-2019" },
  ],
  missing_information: [{ criterion_id: "recent_screening_elsewhere", reason_code: "missing_data", detail: "No outside screening record is on file." }],
  conflicting_information: [{ type: "conflicting_age_at_onset", subject: "mother", values: [46, 52] }],
  source_references: [{ rule_id: "family_history_pathway", references: [{ source_id: "src-1", title: "Example breast screening recommendation", citation: "Example Task Force, 2024", publication_date: "2024-04-30", grade_or_strength: "B" }], metadata: { source_status: "final" } }],
  exclusion_reasons: [],
  evaluation_date: "2026-11-11",
  evaluated_at: "2026-11-11T09:00:00Z",
  execution_version: "engine-1.0",
  guideline: { id: GUIDELINE_B, key: "breast_cancer_screening", name: "Breast cancer screening", version: 2, version_id: "dddddddd-0000-4000-8000-0000000000b3", care_activity: "breast_cancer_screening", priority: "high", review_status: "approved", source_reference: "Example Task Force, 2024", disclaimer: null },
  decisive_rule: { id: "family_history_pathway", rule_version_id: "dddddddd-0000-4000-8000-0000000000b4", name: "Family history pathway", version: 3, rule_type: "inclusion" },
  ...over,
});

const history = [
  { from_status: null, to_status: "identified", actor_role: null, reason: "Rule evaluation", occurred_at: "2026-11-11T09:05:00Z" },
  { from_status: "identified", to_status: "approved_for_planning", actor_role: "care_coordinator", reason: null, occurred_at: "2026-11-11T10:00:00Z" },
  { from_status: "approved_for_planning", to_status: "planned", actor_role: null, reason: "Planning run", occurred_at: "2026-11-11T11:00:00Z" },
];

export const journeyCase = (over: Record<string, unknown> = {}) => ({
  id: CASE_B,
  care_activity: "breast_cancer_screening",
  status: "outreach_pending",
  priority: "high",
  source_outcome: "requires_clinical_review",
  evaluation_id: EVAL_ID,
  planning_run_id: "ffffffff-0000-4000-8000-0000000000b1",
  opened_at: "2026-11-11T09:05:00Z",
  updated_at: "2026-11-11T12:00:00Z",
  closed_at: null,
  closure_reason: null,
  history: [...history, { from_status: "planned", to_status: "outreach_pending", actor_role: null, reason: null, occurred_at: "2026-11-11T12:00:00Z" }],
  allocations: [{ id: "a1", planning_run_id: "ffffffff-0000-4000-8000-0000000000b1", clinic: { id: "c1", key: "pune-urban-1", name: "Pune Urban Clinic 1", district_code: "PUN" }, service_key: "mammography", priority: "high", proposed_start_time: "2026-11-20T09:30:00Z", proposed_end_time: "2026-11-20T10:00:00Z", allocation_status: "proposed", explanation: "Earliest open slot in the resident's district that satisfies the language and priority constraints.", confirmed_at: null, simulated: true }],
  appointments: [],
  communications: [
    { id: "m1", channel: "sms", language: "ar", direction: "rtl", template_key: "screening_reminder", template_version: 1, body: "تذكير: حان موعد فحص الثدي. (رسالة محاكاة)", delivery_state: "delivered", sent_at: "2026-11-12T08:00:00Z", delivered_at: "2026-11-12T08:00:03Z", response_received_at: null, created_at: "2026-11-12T08:00:00Z", responses: [{ id: "r1", response_type: "accepted", response_value: null, received_at: "2026-11-12T09:00:00Z" }], simulated: true },
  ],
  completion_evidence: [],
  ...over,
});

export const journeyPayload = (over: Record<string, unknown> = {}) => ({
  synthetic_banner: "x",
  sources: {},
  resident: { id: R_ID, dataset_version: "syn-20261101-s1", source_patient_id: "p-1", birth_date: "1970-05-02", age: 56, age_band: "50-59", administrative_sex: "female", preferred_language: "ar", district_code: "PUN", deceased_date: null, synthetic_status: "synthetic", age_as_of: "2026-11-11", storage_profile: "full", counts: { clinical_records: 41, family_edges: 3, documents: 3, evidence_facts: 3 } },
  evidence: {
    definition: "x",
    truncated: false,
    counts_by_validation_status: { accepted: 3 },
    facts: [factFor("en"), factFor("ar"), factFor("hi", HI_OVER)],
    documents: [DOCS.en, DOCS.ar, DOCS.hi].map((d) => ({ id: d.id, document_type: "progress_note", language: d.language, created_on: "2026-10-14", registered_at: "2026-11-01T00:00:00Z", processing_status: "extracted" })),
  },
  evaluations: { definition: "x", truncated: false, items: [journeyEval(), journeyEval({ id: "dddddddd-0000-4000-8000-0000000000b9", outcome: "eligible", outcome_class: "eligible", reason_code: null, review_status: "clinically_reviewed", guideline: { ...journeyEval().guideline, key: "cervical_cancer_screening", name: "Cervical cancer screening", care_activity: "cervical_cancer_screening", version: 1 } })] },
  cases: { definition: "x", items: [journeyCase()] },
  restricted: [],
  ...over,
});

export const journeyHandlers = (payload: Record<string, unknown> = journeyPayload()): Record<string, ApiHandler> => ({
  ...conversationHandlers(CASE_B),
  [`GET /residents/${R_ID}/journey`]: () => ({ body: ok(payload) }),
  ...Object.fromEntries((["en", "ar", "hi"] as Lang[]).map((l) => [`GET /evidence/${FACT_IDS[l]}`, () => ({ body: ok(evidenceDetail(l, true, l === "hi" ? HI_OVER : {})) })])),
  [`GET /evaluations/${EVAL_ID}`]: () => ({ body: ok({ ...journeyEval(), resident_id: R_ID, guideline_id: GUIDELINE_B, guideline_key: "breast_cancer_screening", guideline_version_id: "v", guideline_version: 2, rule_version_id: "r", rule_id: "family_history_pathway", rule_version: 3, care_activity: "breast_cancer_screening", input_hash: "h", synthetic_status: "synthetic" }) }),
  "GET /engagement/preview": () => ({ body: ok(previewPayload()) }),
  [`GET /residents/${R_ID}/pedigree`]: () => ({ body: ok(pedigreePayload()) }),
});

export const previewPayload = () => ({
  care_gap_case_id: CASE_B,
  resident_id: R_ID,
  care_activity: "breast_cancer_screening",
  template_key: "screening_reminder",
  template_version: 1,
  resident_preferred_language: "ar",
  default_language: "ar",
  default_language_source: "resident_preference",
  previews: [
    { language: "en", direction: "ltr", activity_label: "breast cancer screening", body: "Reminder: it is time for your breast cancer screening. Reply YES to book a slot. (Simulated message)", characters: 98, constraints_checked: true, is_resident_preferred_language: false, is_default_language: false, template_stored: true, template_approved: true },
    { language: "hi", direction: "ltr", activity_label: "स्तन कैंसर की जांच", body: "अनुस्मारक: आपकी स्तन कैंसर की जांच का समय हो गया है। (सिम्युलेटेड संदेश)", characters: 72, constraints_checked: true, is_resident_preferred_language: false, is_default_language: false, template_stored: true, template_approved: true },
    { language: "ar", direction: "rtl", activity_label: "فحص سرطان الثدي", body: "تذكير: حان موعد فحص سرطان الثدي. (رسالة محاكاة)", characters: 49, constraints_checked: true, is_resident_preferred_language: true, is_default_language: true, template_stored: true, template_approved: true },
  ],
  ai_calls: 0,
  simulated: true,
  notice: "Simulated preview of the approved reminder template: nothing is stored, sent or drafted by an AI model.",
});

export const pedigreePayload = () => ({
  synthetic_banner: "x",
  resident: journeyPayload().resident,
  definition: "Relatives are exactly the stored family_edges rows.",
  relatives: [
    {
      edge_id: "e1", relationship: "mother", degree: 1, degree_label: "first-degree", blood_relative: true, direction: "related_to_resident", reading: "The related person is the resident's mother.",
      linked_resident: { id: "cccccccc-0000-4000-8000-0000000000c1", age: 81, administrative_sex: "female", district_code: "PUN", deceased: false }, related_person_source_id: null, source_type: "seeded_family_linker", confidence_status: "verified",
      cancer_history: { clinical_records: [{ id: "cr1", display: "Malignant neoplasm of breast", code: "254837009", status: "active", effective_start: "2009-03-01", source_reference: "Condition/abc" }], evidence_facts: [{ ...factFor("en"), basis: "stated_in_resident_document" }] },
    },
    { edge_id: "e2", relationship: "father", degree: 1, degree_label: "first-degree", blood_relative: true, direction: "related_to_resident", reading: "The related person is the resident's father.", linked_resident: null, related_person_source_id: "src-father-1", source_type: "seeded_family_linker", confidence_status: "verified", cancer_history: { clinical_records: [], evidence_facts: [] } },
    { edge_id: "e3", relationship: "maternal_aunt", degree: 2, degree_label: "second-degree", blood_relative: true, direction: "related_to_resident", reading: "The related person is the resident's maternal aunt.", linked_resident: { id: "cccccccc-0000-4000-8000-0000000000c3", age: 74, administrative_sex: "female", district_code: "PUN", deceased: true }, related_person_source_id: null, source_type: "seeded_family_linker", confidence_status: "verified", cancer_history: { clinical_records: [], evidence_facts: [{ ...factFor("ar", { relationship_type: "maternal_aunt" }), basis: "relative_own_document" }] } },
    { edge_id: "e4", relationship: "sister", degree: 1, degree_label: "first-degree", blood_relative: true, direction: "related_to_resident", reading: "The related person is the resident's sister.", linked_resident: { id: "cccccccc-0000-4000-8000-0000000000c4", age: 52, administrative_sex: "female", district_code: "PUN", deceased: false }, related_person_source_id: null, source_type: "seeded_family_linker", confidence_status: "verified", cancer_history: { clinical_records: null, evidence_facts: null } },
  ],
  unattributed_family_facts: [],
  unattributed_note: "Accepted cancer-related family-member facts that cannot be tied to exactly one stored relative.",
  counts: { relatives: 4, linked_relatives: 3, relatives_with_cancer_history: 2 },
  restricted: [],
});

const stage = (key: string, label: string, count: number, definition: string, source: string, prev: number | null, total: number, cases: number | null = null) => ({
  key, label, count, cases, unit: "residents", share_of_residents: count / total, conversion_from_previous: prev == null ? null : count / prev, definition, source,
});

export const funnelPayload = () => ({
  generated_at: "2026-11-12T08:00:00Z",
  cache_ttl_seconds: 15,
  synthetic_banner: "x",
  sources: {},
  note: "Each stage is an independent count of distinct residents. After a re-evaluation a later stage can exceed an earlier one.",
  stages: [
    stage("residents", "Residents", 200000, "Rows in residents.", "synthetic", null, 200000),
    stage("evaluated", "Evaluated", 150000, "Distinct residents with at least one stored eligibility evaluation.", "synthetic", 200000, 200000),
    stage("due", "Due", 30000, "Distinct residents whose latest evaluation is eligible or requires clinical review.", "synthetic", 150000, 200000),
    stage("case_opened", "Care gap case opened", 24000, "Distinct residents with at least one care gap case ever created.", "synthetic", 30000, 200000, 26000),
    stage("planned", "Planned", 12000, "Distinct residents with a case that is or was planned.", "simulated", 24000, 200000, 12500),
    stage("in_outreach", "In outreach", 6000, "Distinct residents with a case in outreach or beyond.", "simulated", 12000, 200000, 6100),
    stage("closed", "Closed", 900, "Distinct residents with a completed case.", "simulated", 6000, 200000, 905),
  ],
  leakage: { definition: "Cases now in a status that leaves the funnel without completion.", source: "simulated", items: [{ status: "closed_not_completed", cases: 120 }, { status: "cancelled", cases: 40 }, { status: "escalated", cases: 75 }] },
});
