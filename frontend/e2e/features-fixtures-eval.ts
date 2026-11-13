// Evaluation run fixture (shape of backend/anticipa/evaluation runner, report and stats). Invented demo values for tests
// only: they are not measurements of anything.

const rate = (value: number | null, n: number, over: Record<string, unknown> = {}) =>
  value == null
    ? { value: null, numerator: 0, denominator: 0, n: 0, unit: "ratio", ci95: null, status: "not_applicable", flags: [] }
    : { value, numerator: Math.round(value * n), denominator: n, n, unit: "ratio", ci95: [Math.max(0, value - 0.03), Math.min(1, value + 0.02)], status: "measured", flags: n < 30 ? ["low_n"] : [], ...over };
const byLang = (en: number | null, hi: number | null, ar: number | null) => ({ language: { en: rate(en, 400), hi: rate(hi, 220), ar: rate(ar, 18) } });
const byCat = (a: number, b: number) => ({ category: { family_history: rate(a, 300), screening: rate(b, 150) } });

export const evalRun = (over: Record<string, unknown> = {}) => ({
  run_id: "20261112T080000-ab12cd34",
  status: "complete",
  run_type: "dev",
  dataset_version: "syn-20261101-s1",
  split: "dev",
  cohort_id: "cohort-1",
  ground_truth_id: "gt-1",
  gate_summary: { pass: 5, fail: 1, warn: 1, not_evaluated: 2, skipped: 0 },
  metrics_digest: "abc",
  started_at: "2026-11-12T08:00:00Z",
  completed_at: "2026-11-12T08:20:00Z",
  synthetic_status: "synthetic",
  synthetic_banner: "All data is synthetic or simulated. Not clinical validation.",
  model_versions: [{ provider: "anthropic", model_id: "test-model", prompt_version: "evidence.extract.v3" }],
  code: { git_commit: "0123456789abcdef", dirty: false },
  metrics: {
    "EX-01.A": { ...rate(0.962, 640), breakdowns: { ...byLang(0.97, 0.95, 0.9), ...byCat(0.97, 0.94) } },
    "EX-02.A": { ...rate(0.81, 640), breakdowns: { ...byLang(0.84, 0.78, null), ...byCat(0.85, 0.74) } },
    "EX-03.A": { ...rate(0.88, 640), breakdowns: { ...byLang(0.9, 0.86, 0.8), ...byCat(0.9, 0.83) } },
    "EX-06": { ...rate(0.991, 700), breakdowns: byLang(0.995, 0.99, 0.95) },
    "EX-08a": { ...rate(0.985, 600), breakdowns: byLang(0.99, 0.98, 0.94) },
    "EX-09": { ...rate(0.97, 600), breakdowns: byLang(0.98, 0.96, 0.9) },
    "EX-12": { ...rate(0.012, 640), breakdowns: byLang(0.01, 0.015, 0.05) },
    "EX-13": { value: null, status: "not_evaluated", reason: "No person has reviewed a sample, so the adjudicated rate was not measured." },
    "SAF-02": { ...rate(0.004, 640), count: 3, metric: "negation flip rate (accepted facts)" },
    "SAF-05": { ...rate(0, 640), count: 0, metric: "accepted facts lacking a verified quote" },
    "PF-01": {
      value: 4.2,
      unit: "USD per 1,000 documents",
      status: "measured",
      n: 500,
      total_cost_usd: 2.1,
      calls: 520,
      cost_basis: "estimated_from_config_prices (not reconciled with a provider bill)",
      by_provider_model_task: { "anthropic/test-model/evidence.extract": { calls: 520, cost_usd: 2.1, input_tokens: 410000, output_tokens: 52000 } },
    },
    "PF-04": { overall: { n: 480, p50_ms: 812, p95_ms: 2210, mean_ms: 1000, flags: [] }, value: null, status: "measured", unit: "ms" },
  } as Record<string, unknown>,
  gates: [
    { gate_id: "SG-01", tier: 3, metric_id: "EX-06", comparator: ">=", threshold: 0.95, threshold_per_language: 0.9, threshold_status: "proposal", observed: 0.991, observed_by_language: { en: 0.995, hi: 0.99, ar: 0.95 }, result: "pass", severity: "block", description: "quote verification rate (candidate view)" },
    { gate_id: "SG-08a", tier: 3, metric_id: "EX-12", comparator: "<=", threshold: 0.02, threshold_per_language: 0.03, threshold_status: "proposal", observed: 0.012, observed_by_language: { en: 0.01, hi: 0.015, ar: 0.05 }, result: "fail", severity: "block", description: "hallucinated fact rate (strict)" },
    { gate_id: "INV-02", tier: 1, metric_id: "SAF-05", comparator: "==", threshold: 0, threshold_status: "confirmed (zero tolerance)", observed: 0, result: "pass", severity: "block", description: "accepted facts lacking a verified quote" },
    { gate_id: "SG-17", tier: 3, metric_id: "PF-04", comparator: "<=", threshold: null, threshold_status: "proposal", observed: null, result: "not_evaluated", severity: "block", description: "latency budget (undefined)" },
  ],
  coverage: [
    { tier: "synthetic_population", target: 200000, actual_completed: 200000, note: "x" },
    { tier: "ai_evidence_cohort", target: 10000, actual_completed: 2400, note: "x" },
  ],
  exclusions: [{ item: "documents not yet processed", reason: "120" }],
  failures: { documents_failed: 4, by_error_category: { rate_limited: 3, output_validation: 1 } },
  limitations: ["Synthetic text is not clinical text: results do not transfer to real data.", "Cost is estimated from configured prices."],
  ...over,
});

export const evalRunsList = () => [
  { run_id: "20261112T080000-ab12cd34", status: "complete", run_type: "dev", dataset_version: "syn-20261101-s1", split: "dev", gate_summary: {}, started_at: "2026-11-12T08:00:00Z", completed_at: "2026-11-12T08:20:00Z" },
  { run_id: "20261111T080000-ee12cd34", status: "complete", run_type: "smoke", dataset_version: "syn-20261101-s1", split: "smoke", gate_summary: {}, started_at: "2026-11-11T08:00:00Z", completed_at: "2026-11-11T08:05:00Z" },
];
