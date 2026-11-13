// GET /analytics/impact, trimmed to the fields the Impact and economics tab reads. Invented values for tests only.

const truthSide = (found: number, missed: number, wrongCall: number, due: number, notDue: number) => ({
  truly_due: { found_as_due: found, missed_as_not_due: missed, pairs: due, sent_to_review: due - found - missed },
  truly_not_due: { correctly_not_due: notDue - wrongCall, pairs: notDue, sent_to_review: 0, unnecessary_outreach: wrongCall },
});
const plan = (allocated: number, rate: number, own: number) => ({
  cases: 86988,
  allocated,
  unallocated: 86988 - allocated,
  seats_total: 168045,
  seats_services_with_cases: 58347,
  allocation_rate: { numerator: allocated, denominator: 86988, value: rate },
  seat_utilisation_services_with_cases: { numerator: allocated, denominator: 58347, value: 0.897 },
  travel: { own_district_share: own, mean_tier: 0.29 },
  wait_days_from_horizon_start: { mean: 7.5, median: 6.625, p90: 12.7 },
  hard_constraint_violations: { count: 0 },
  coverage_by_service: { cervical_screening: { allocated: 23713, coverage: 0.724, demand: 32751 }, mammography: { allocated: 14485, coverage: 0.494, demand: 29309 } },
});
const uplift = (n: number) => ({ case_opened_only_because_of_notes: n, case_pairs_rules_only: 0, case_pairs_with_notes: n, changes: {}, pairs: 10000 });

export const impactReport = () => ({
  schema_version: "anticipa.impact.v1",
  generated_at: "2026-11-13T07:13:18+00:00",
  limitations: [
    "Residents, notes and clinic capacity are synthetic. The baselines show how the mechanism behaves on generated data, not a benefit in a real population.",
    "Capacity is simulated. Both plans fill every seat of the screening services, so equal totals follow from seats being the limit.",
    "No outcome such as deaths averted or money saved is estimated: the project holds no benefit model or cited parameter for it.",
  ],
  definitions: {
    found_as_due: "Of truly due pairs, those the approach returned as eligible.",
    "equity gap_points": "Highest minus lowest coverage, in percentage points, among groups with at least min_group_size cases.",
  },
  measured_inputs: {
    residents: 200000,
    care_gap_cases: 116472,
    residents_with_a_case: 71593,
    ai_cohort: { residents: 10000, documents_extracted: 32523, evidence_facts: 126371, evidence_facts_per_resident: 12.6 },
    ai_extraction_cost: { calls: 33173, documents: 32523, total_cost_usd: 26.889285, escalation_cost_usd: 10.868214, usd_per_1000_documents: 0.826778, usd_per_1000_residents_of_the_ai_cohort: 2.688928, cost_basis: "estimated from configured prices" },
  },
  rules_baseline: {
    scope: { residents: 10000, pairs: 60000, evaluation_date: "2026-11-12", ground_truth: { dev_residents: 1969 } },
    uplift_from_reading_notes: {
      all: { ...uplift(1914), residents_with_case_opened_only_because_of_notes: 1829, pairs: 60000, changes: { paused_for_missing_information: 1497 } },
      brca_genetic_counseling_referral: uplift(1184),
      brca_risk_assessment: uplift(363),
      cervical_cancer_screening: uplift(0),
    },
    against_planted_evidence_truth: { pairs: 11814, rules_only: truthSide(211, 366, 45, 577, 10521), with_notes: truthSide(562, 5, 2, 577, 10521) },
    against_planted_screening_truth: { pairs: 5102, rules_only: truthSide(98, 13, 269, 111, 4754), with_notes: truthSide(18, 20, 3, 111, 4754) },
  },
  planner_baseline: {
    first_come_first_served: plan(52352, 0.60183, 0.707),
    ortools_cpsat: plan(52352, 0.60183, 0.744),
    difference: {
      district_equity_gap_points: { first_come: 12.935342, ortools: 1.111827 },
      language_equity_gap_points: { first_come: 0.484185, ortools: 0.029051 },
      own_district_share: { first_come: 0.707385, ortools: 0.743716 },
      median_wait_days: { first_come: 6.625, ortools: 6.625 },
      hard_constraint_violations: { first_come: 0, ortools: 0 },
    },
    inputs: { cases: 86988, distinct_residents: 59181, usable_seats: 168045 },
  },
});
