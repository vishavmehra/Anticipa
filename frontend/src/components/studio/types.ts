import type { GuidelineVersion } from "@/lib/types";

// Shapes of the Guideline Studio API (backend/anticipa/guidelines/{routes,dryrun}.py). A dry run never saves anything.

/** GET /guidelines/{id}/versions/{vid}: the version with its rules. `version_metadata` is not in the generated types yet. */
export type VersionFull = GuidelineVersion & { version_metadata?: Record<string, unknown> | null };

export interface DryRunExample {
  resident_id: string;
  change: "newly_due" | "no_longer_due" | "changed_other" | "unchanged" | "eligible" | "other" | string;
  outcome: string;
  reason_code: string | null;
  reason: string | null;
  baseline_outcome: string | null;
  baseline_reason_code: string | null;
  district: string;
  age_band: string;
  sex: string;
}

export interface Interval {
  estimate: number;
  low: number;
  high: number;
}

export interface DryRunDiff {
  eligible_baseline: number;
  eligible_version: number;
  newly_due: number;
  no_longer_due: number;
  changed_other: number;
  unchanged: number;
  newly_due_by_district: Record<string, number>;
  newly_due_by_age_band: Record<string, number>;
  newly_due_by_sex: Record<string, number>;
  no_longer_due_by_district: Record<string, number>;
  transitions: { from: string; to: string; count: number }[];
}

export interface VersionMeta {
  id: string;
  version: number;
  review_status: string;
  content_hash: string;
  care_activity: string;
}

export interface DryRunResult {
  persisted: false;
  engine_version: string;
  guideline: { id: string; key: string; name: string };
  version: VersionMeta;
  baseline: VersionMeta | null;
  evaluation_date: string;
  sample: {
    type: "ai_cohort" | "random_n";
    requested: number | null;
    evaluated: number;
    cohort_id: string | null;
    district: string | null;
    seed: number;
    population_residents: number;
  };
  runtime_seconds: number;
  residents_per_second: number | null;
  residents_with_errors: number;
  outcomes: Record<string, number>;
  baseline_outcomes: Record<string, number> | null;
  by_district: Record<string, Record<string, number>>;
  by_age_band: Record<string, Record<string, number>>;
  by_sex: Record<string, Record<string, number>>;
  district_names: Record<string, { name: string; state: string }>;
  diff: DryRunDiff | null;
  top_reason_codes: { reason_code: string; outcome: string; count: number; baseline_count: number | null }[];
  examples: DryRunExample[];
  estimate: {
    basis: string;
    population_residents: number;
    eligible: Interval;
    newly_due: Interval | null;
    no_longer_due: Interval | null;
  } | null;
  notes: string[];
}

export interface DryRunJob {
  id: string;
  version_id: string;
  status: "queued" | "pending" | "running" | "completed" | "partially_completed" | "failed" | "cancelled";
  scope: Record<string, unknown>;
  total_items: number | null;
  processed_items: number;
  failed_items: number;
  error_summary: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  result: DryRunResult | null;
}

export const TERMINAL = new Set(["completed", "partially_completed", "failed", "cancelled"]);
