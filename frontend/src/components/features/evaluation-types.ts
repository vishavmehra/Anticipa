// Shape of GET /analytics/evaluation and /analytics/evaluation/runs/{id} (backend/anticipa/evaluation/routes.py `_full`,
// runner.py report assembly, stats.py `rate`). Every field is optional: a partial or older run still renders, and a
// missing value is shown as "Not measured", never as zero. Callers: evaluation.tsx.

export interface Rate {
  value?: number | null;
  numerator?: number;
  denominator?: number;
  n?: number;
  unit?: string;
  ci95?: [number, number] | null;
  status?: string; // measured | not_applicable | not_evaluated
  flags?: string[]; // low_n, ...
  metric?: string; // human description of the metric, when the backend sends one
  reason?: string; // why a metric was not measured
  count?: number; // SAF metrics
  breakdowns?: Record<string, Record<string, Rate>>; // language | category | scenario_id | ...
  // PF-01
  total_cost_usd?: number;
  calls?: number;
  cost_basis?: string;
  by_provider_model_task?: Record<string, { calls: number; cost_usd: number; input_tokens: number; output_tokens: number }>;
  // PF-04
  overall?: { n: number; p50_ms: number | null; p95_ms: number | null; mean_ms: number | null; flags?: string[] };
}

export interface Gate {
  gate_id: string;
  tier?: number;
  metric_id: string;
  comparator: string;
  threshold: number | boolean | null;
  threshold_per_language?: number | null;
  threshold_status?: string;
  observed: number | boolean | null;
  observed_by_language?: Record<string, number | null>;
  result: string; // pass | fail | warn | not_evaluated | skipped
  severity?: string;
  description?: string;
}

export interface EvaluationRun {
  run_id: string;
  status: string;
  run_type: string;
  dataset_version: string;
  split: string;
  cohort_id: string | null;
  ground_truth_id: string | null;
  gate_summary: Record<string, number>;
  metrics_digest: string | null;
  started_at: string;
  completed_at: string | null;
  metrics?: Record<string, Rate>;
  gates?: Gate[];
  model_versions?: { provider: string; model_id: string; prompt_version: string | null }[];
  prompt_versions?: string[];
  coverage?: { tier: string; target: number; actual_completed: number; note?: string }[];
  code?: { git_commit?: string | null; dirty?: boolean | null };
  safety_cases?: Record<string, unknown>;
  exclusions?: { item: string; reason: string }[];
  failures?: { documents_failed?: number; by_error_category?: Record<string, number> };
  limitations?: string[];
  projections?: { name: string; formula?: string; value: number }[];
}

/** GET /analytics/evaluation/runs item (metadata and gate summary only). */
export type EvaluationRunSummary = Pick<EvaluationRun, "run_id" | "status" | "run_type" | "dataset_version" | "split" | "gate_summary" | "started_at" | "completed_at">;
