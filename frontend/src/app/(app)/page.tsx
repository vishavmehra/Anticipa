"use client";

import { api, isMissingEndpoint } from "@/lib/api";
import { fmtDateTime, fmtInt } from "@/lib/format";
import type { PopulationOverview } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { SyntheticBadge } from "@/components/badges";
import { StatTile } from "@/components/stat-tile";
import { ErrorPanel, PageHeader, Section, Unavailable, btnSecondary } from "@/components/ui";

const WILL_SHOW = [
  "Residents generated, ingested and normalised, with structured-rule and AI evidence-processing coverage.",
  "Document processing counts.",
  "Every metric with its numerator, denominator and source; targets shown apart from measured values.",
];

export default function PopulationPage() {
  const res = useApi("population-overview", (signal) => api<PopulationOverview>("/analytics/population-overview", { signal }));
  const o = res.data?.data;
  const loading = res.loading && !o;
  const of = (n: number | null | undefined) =>
    o?.generated_residents != null && n != null ? `of ${fmtInt(o.generated_residents)} generated residents` : "residents";
  const docs = o?.documents;

  return (
    <>
      <PageHeader
        title="Population"
        description="Command center for preventive-care operations across the synthetic resident population. Figures are measured counts, never targets."
        actions={
          <button type="button" className={btnSecondary} onClick={res.reload} disabled={res.loading}>
            Refresh
          </button>
        }
      />
      {res.error ? (
        isMissingEndpoint(res.error) ? (
          <Unavailable endpoint="GET /api/v1/analytics/population-overview" willShow={WILL_SHOW} />
        ) : (
          <ErrorPanel error={res.error} onRetry={res.reload} />
        )
      ) : (
        <>
          <p className="mb-4 text-ink-muted">
            {o?.dataset_version ? `Dataset ${o.dataset_version}` : "Dataset not reported"}
            {o?.as_of ? `, as of ${fmtDateTime(o.as_of)}` : ""}.
          </p>
          <Section id="pipeline" title="Processing coverage">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
              <StatTile loading={loading} label="Generated" value={o ? fmtOrNull(o.generated_residents) : null} basis="synthetic residents in the dataset" badge={<SyntheticBadge />} />
              <StatTile loading={loading} label="Ingested" value={o ? fmtOrNull(o.ingested_residents) : null} basis={of(o?.ingested_residents)} badge={<SyntheticBadge />} />
              <StatTile loading={loading} label="Normalised" value={o ? fmtOrNull(o.normalized_residents) : null} basis={of(o?.normalized_residents)} badge={<SyntheticBadge />} />
              <StatTile loading={loading} label="Rules evaluated" value={o ? fmtOrNull(o.rule_evaluated_residents) : null} basis={of(o?.rule_evaluated_residents)} badge={<SyntheticBadge />} />
              <StatTile loading={loading} label="AI evidence processed" value={o ? fmtOrNull(o.ai_processed_residents) : null} basis={of(o?.ai_processed_residents)} badge={<SyntheticBadge />} />
            </div>
          </Section>
          <Section id="documents" title="Documents">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile loading={loading} label="Registered" value={o ? fmtOrNull(docs?.registered) : null} basis="synthetic documents" badge={<SyntheticBadge />} />
              <StatTile loading={loading} label="Processed" value={o ? fmtOrNull(docs?.processed) : null} basis="evidence extraction finished" />
              <StatTile loading={loading} label="Pending" value={o ? fmtOrNull(docs?.pending) : null} basis="awaiting extraction" />
              <StatTile loading={loading} label="Failed" value={o ? fmtOrNull(docs?.failed) : null} basis="extraction failed" />
            </div>
          </Section>
        </>
      )}
    </>
  );
}

/** A missing measurement stays null so StatTile says "Not available" instead of showing a made-up zero. */
const fmtOrNull = (n: number | null | undefined) => (n == null ? null : fmtInt(n));
