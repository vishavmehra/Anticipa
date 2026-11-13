"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, api, apiPost } from "@/lib/api";
import { displayText } from "@/lib/display";
import { fmtDate, fmtDateTime, fmtInt, humanize } from "@/lib/format";
import { useDistrictMap } from "@/lib/geography";
import { PERM } from "@/lib/nav";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { GatedButton } from "@/components/gated-button";
import { StatusBadge } from "@/components/status-badge";
import { BarList, Card, CardHeader, ErrorPanel, FilterField, Icon, KpiTile, Pill, ProgressBar, btnPrimary, selectClass, type Tone } from "@/components/ui";
import { plain } from "./criteria";
import { type DryRunExample, type DryRunJob, type DryRunResult, TERMINAL } from "./types";

const OUTCOME_ORDER = ["eligible", "not_eligible", "already_completed", "insufficient_evidence", "requires_clinical_review", "temporarily_excluded", "evaluation_error"];
const SIZES = [1000, 2500, 5000, 10000] as const;
const BAND_ORDER = ["0-17", "18-29", "30-39", "40-49", "50-59", "60-69", "70+", "70-79", "80+", "unknown"];
const bandLabel = (b: string) => b.replace(/^(\d+)-(\d+)$/, "$1 to $2").replace(/^(\d+)\+$/, "$1 and over").replace("unknown", "Unknown");

const CHANGE: Record<string, { label: string; tone: Tone }> = {
  newly_due: { label: "Newly due", tone: "ok" },
  no_longer_due: { label: "No longer due", tone: "warn" },
  changed_other: { label: "Outcome changed", tone: "info" },
  unchanged: { label: "No change", tone: "neutral" },
  eligible: { label: "Due", tone: "ok" },
  other: { label: "Not due", tone: "neutral" },
};

// ---- headline ---------------------------------------------------------------------------------------------------------
function districtName(r: DryRunResult, code: string) {
  return r.district_names[code]?.name ?? code;
}

/** "+1,240 residents newly due, mainly Bhopal and Indore" from the diff; counts are of the sample unless an estimate is given. */
function headline(r: DryRunResult): string {
  const d = r.diff;
  if (!d) return `${fmtInt(r.outcomes.eligible ?? 0)} of ${fmtInt(r.sample.evaluated)} residents in the sample are due under this version`;
  const lost = `${fmtInt(d.no_longer_due)} ${d.no_longer_due === 1 ? "resident" : "residents"} would no longer be due`;
  if (d.newly_due === 0) return d.no_longer_due > 0 ? lost : "No resident changes status in this sample";
  const ranked = Object.entries(d.newly_due_by_district).sort((a, b) => b[1] - a[1]);
  const top = ranked.slice(0, 2);
  const share = top.reduce((t, [, n]) => t + n, 0) / d.newly_due;
  let where = "";
  if (ranked.length === 1) where = `, all in ${districtName(r, ranked[0][0])}`;
  else if (ranked.length > 1 && share >= 0.5) where = `, mainly ${top.map(([c]) => districtName(r, c)).join(" and ")}`;
  else if (ranked.length > 1) where = `, spread across ${ranked.length} districts`;
  return `+${fmtInt(d.newly_due)} ${d.newly_due === 1 ? "resident" : "residents"} newly due${where}`;
}

function estimateLine(r: DryRunResult): string | null {
  const e = r.estimate;
  if (!e) return null;
  const pick = e.newly_due && r.diff && r.diff.newly_due > 0 ? e.newly_due : e.eligible;
  const what = pick === e.newly_due ? "newly due" : "due";
  return `About ${fmtInt(pick.estimate)} residents ${what} across the ${fmtInt(e.population_residents)} in scope (95 percent interval ${fmtInt(pick.low)} to ${fmtInt(pick.high)}).`;
}

// ---- results ----------------------------------------------------------------------------------------------------------
function rowsFrom(counts: Record<string, number>, label: (k: string) => string, order?: string[]) {
  const rows = Object.entries(counts).map(([k, v]) => ({ label: label(k), value: v, key: k }));
  return order ? rows.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key)) : rows.sort((a, b) => b.value - a.value);
}

function Example({ e, r }: { e: DryRunExample; r: DryRunResult }) {
  const c = CHANGE[e.change] ?? { label: humanize(e.change), tone: "neutral" as Tone };
  const short = e.resident_id.slice(0, 8);
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1.5 rounded-control border border-line px-4 py-2.5">
      <Link href={`/residents/${e.resident_id}/journey`} className="font-mono text-sm text-brand-ink underline underline-offset-2" aria-label={`Open the journey of resident ${short}`}>
        {short}
      </Link>
      <Pill tone={c.tone}>{c.label}</Pill>
      <span className="text-sm font-medium">{humanize(e.outcome)}</span>
      {e.baseline_outcome && e.baseline_outcome !== e.outcome && <span className="text-sm text-ink-muted">was {humanize(e.baseline_outcome).toLowerCase()}</span>}
      <span className="text-sm text-ink-muted">
        {districtName(r, e.district)}, {bandLabel(e.age_band)}, {humanize(e.sex)}
      </span>
      {(e.reason || e.reason_code) && <p className="w-full text-[0.8125rem] text-ink-soft">{e.reason ? plain(e.reason) : humanize(e.reason_code)}</p>}
    </li>
  );
}

function Results({ job, r, currentHash }: { job: DryRunJob; r: DryRunResult; currentHash: string | null }) {
  const d = r.diff;
  const n = r.sample.evaluated;
  const byGroup = (counts: Record<string, Record<string, number>>) => Object.fromEntries(Object.entries(counts).map(([k, v]) => [k, v.eligible ?? 0]));
  const districtCounts = d && d.newly_due > 0 ? d.newly_due_by_district : byGroup(r.by_district);
  const bandCounts = d && d.newly_due > 0 ? d.newly_due_by_age_band : byGroup(r.by_age_band);
  const lead = d && d.newly_due > 0 ? "Newly due" : "Due";
  const outcomeRows = OUTCOME_ORDER.filter((o) => (r.outcomes[o] ?? 0) > 0 || (r.baseline_outcomes?.[o] ?? 0) > 0).map((o) => ({
    label: humanize(o),
    value: r.outcomes[o] ?? 0,
    display: fmtInt(r.outcomes[o] ?? 0),
    note: r.baseline_outcomes ? `was ${fmtInt(r.baseline_outcomes[o] ?? 0)}` : undefined,
  }));
  const est = estimateLine(r);
  const sampleText = r.sample.type === "ai_cohort" ? `the AI cohort (${fmtInt(n)} residents)` : `a random sample of ${fmtInt(n)} of ${fmtInt(r.sample.population_residents)} residents`;
  const where = r.sample.district ? ` in ${districtName(r, r.sample.district)}` : "";

  return (
    <div className="space-y-5" data-testid="dry-run-results">
      <Card as="section" aria-labelledby="dr-head" className="glass-strong px-5 py-5">
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone="brand" icon="eye">
            Preview, nothing saved
          </Pill>
          {currentHash && r.version.content_hash !== currentHash && (
            <Pill tone="warn" icon="clock">
              Out of date: the rules changed after this run
            </Pill>
          )}
          <span className="text-xs text-ink-muted">
            Version {r.version.version} against {r.baseline ? `approved version ${r.baseline.version}` : "no other approved version"}
          </span>
        </div>
        <h3 id="dr-head" className="mt-2 font-display text-[1.7rem] leading-9 font-bold tracking-tight text-ink [overflow-wrap:anywhere]">
          {headline(r)}
        </h3>
        {d && d.newly_due > 0 && d.no_longer_due > 0 && <p className="mt-1 text-ink-soft">{fmtInt(d.no_longer_due)} residents would no longer be due.</p>}
        {est && <p className="mt-1 text-ink-soft">{est}</p>}
        <p className="mt-2 max-w-[75ch] text-[0.8125rem] text-ink-muted">
          Evaluated {sampleText}
          {where}, as of {fmtDate(r.evaluation_date)}, in {r.runtime_seconds.toLocaleString("en-US", { maximumFractionDigits: 1 })} seconds. Run {fmtDateTime(job.created_at)}.
          {r.sample.type === "ai_cohort" ? " The AI cohort is a stratified sample, so these counts are not scaled to the whole population." : ""}
        </p>
        {r.residents_with_errors > 0 && <p className="mt-1 text-[0.8125rem] font-semibold text-danger">{fmtInt(r.residents_with_errors)} residents could not be evaluated.</p>}
      </Card>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" role="region" aria-label="Dry run totals">
        {d ? (
          <>
            <KpiTile label="Due under the approved version" value={fmtInt(d.eligible_baseline)} icon="clipboard-check" basis={`of ${fmtInt(n)} in the sample`} />
            <KpiTile label="Due under this version" value={fmtInt(d.eligible_version)} icon="target" tint="violet" basis={`of ${fmtInt(n)} in the sample`} />
            <KpiTile label="Newly due" value={fmtInt(d.newly_due)} icon="trend-up" tint="green" basis="Not due before, due now" />
            <KpiTile label="No longer due" value={fmtInt(d.no_longer_due)} icon="trend-down" tint="amber" basis="Due before, not due now" />
          </>
        ) : (
          <>
            <KpiTile label="Residents in the sample" value={fmtInt(n)} icon="users" basis={r.sample.type === "ai_cohort" ? "AI cohort" : "Random sample"} />
            <KpiTile label="Due under this version" value={fmtInt(r.outcomes.eligible ?? 0)} icon="target" tint="violet" basis={`of ${fmtInt(n)} in the sample`} />
          </>
        )}
      </div>

      <div className="grid gap-5 lg:grid-cols-2 *:min-w-0">
        <Card as="section" aria-labelledby="dr-outcomes">
          <CardHeader id="dr-outcomes" icon="chart" title="Outcomes under this version" subtitle={r.baseline_outcomes ? "Compared with the approved version" : undefined} />
          <div className="px-5 pb-5">
            <BarList rows={outcomeRows} max={Math.max(1, n)} ariaLabel="Residents by outcome" tableCaption="Residents by outcome under this version" valueHeader="Residents" labelWidth="w-24 sm:w-40" />
          </div>
        </Card>
        <Card as="section" aria-labelledby="dr-where">
          <CardHeader id="dr-where" icon="pin" title={`${lead} by district`} />
          <div className="px-5 pb-5">
            {Object.keys(districtCounts).length === 0 ? (
              <p className="text-ink-muted">No residents in the sample.</p>
            ) : (
              <BarList rows={rowsFrom(districtCounts, (c) => districtName(r, c)).slice(0, 10)} ariaLabel={`${lead} residents by district`} tableCaption={`${lead} residents by district`} valueHeader="Residents" labelWidth="w-24 sm:w-36" />
            )}
          </div>
        </Card>
        <Card as="section" aria-labelledby="dr-bands">
          <CardHeader id="dr-bands" icon="users" title={`${lead} by age band`} />
          <div className="px-5 pb-5">
            {Object.keys(bandCounts).length === 0 ? (
              <p className="text-ink-muted">No residents in the sample.</p>
            ) : (
              <BarList rows={rowsFrom(bandCounts, bandLabel, BAND_ORDER)} ariaLabel={`${lead} residents by age band`} tableCaption={`${lead} residents by age band`} valueHeader="Residents" labelWidth="w-24 sm:w-28" />
            )}
          </div>
        </Card>
        <Card as="section" aria-labelledby="dr-reasons">
          <CardHeader id="dr-reasons" icon="scale" title="Top reasons" subtitle="Why residents are not due, under this version" />
          <div className="px-5 pb-5">
            {r.top_reason_codes.length === 0 ? (
              <p className="text-ink-muted">Every evaluated resident is due.</p>
            ) : (
              <div role="region" aria-label="Top reasons table" tabIndex={0} className="overflow-x-auto">
                <table className="w-full text-sm">
                  <caption className="sr-only">Reasons residents are not due, with counts under this version and the approved version</caption>
                  <thead className="text-left text-xs text-ink-muted">
                    <tr>
                      <th scope="col" className="py-1.5 pr-3 font-semibold">Reason</th>
                      <th scope="col" className="py-1.5 pr-3 font-semibold">Outcome</th>
                      <th scope="col" className="py-1.5 pr-3 text-right font-semibold">Residents</th>
                      {d && <th scope="col" className="py-1.5 text-right font-semibold">Before</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {r.top_reason_codes.map((x) => (
                      <tr key={`${x.outcome}:${x.reason_code}`} className="border-t border-line align-top">
                        <td className="py-1.5 pr-3">{humanize(x.reason_code)}</td>
                        <td className="py-1.5 pr-3 text-ink-soft">{humanize(x.outcome)}</td>
                        <td className="tnum py-1.5 pr-3 text-right font-semibold">{fmtInt(x.count)}</td>
                        {d && <td className="tnum py-1.5 text-right text-ink-muted">{fmtInt(x.baseline_count ?? 0)}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </Card>
      </div>

      <Card as="section" aria-labelledby="dr-examples">
        <CardHeader id="dr-examples" icon="search" title="Example residents" subtitle="Residents the change moves first, then a few it does not. Open one to read their journey." />
        <ul aria-label="Example residents" className="grid gap-2 px-5 pb-5 lg:grid-cols-2">
          {r.examples.map((e) => (
            <Example key={e.resident_id} e={e} r={r} />
          ))}
        </ul>
      </Card>

      <ul className="space-y-1 text-[0.8125rem] text-ink-muted">
        {r.notes.map((t) => (
          <li key={t}>{displayText(t)}</li>
        ))}
      </ul>
    </div>
  );
}

// ---- progress ---------------------------------------------------------------------------------------------------------
/** Polls one dry run until it ends. Keyed by job id by the caller, so a new run starts from a clean state. */
function JobView({ guidelineId, versionId, id, currentHash }: { guidelineId: string; versionId: string; id: string; currentHash: string | null }) {
  const [job, setJob] = useState<DryRunJob | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const res = await api<DryRunJob>(`/guidelines/${guidelineId}/versions/${versionId}/dry-runs/${id}`);
        if (stop) return;
        setJob(res.data);
        setError(null);
        if (!TERMINAL.has(res.data.status)) timer = setTimeout(tick, 1500);
      } catch (e) {
        if (stop) return;
        setError(e instanceof ApiError ? e : new ApiError("INTERNAL_ERROR", "Unexpected client error.", 0));
        timer = setTimeout(tick, 4000);
      }
    };
    void tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [guidelineId, versionId, id]);

  if (error && !job) return <ErrorPanel error={error} title="Could not read this dry run" />;
  if (!job) return <div className="skeleton h-24" role="status" aria-label="Loading the dry run" />;
  if (!TERMINAL.has(job.status)) {
    return (
      <Card as="section" aria-label="Dry run progress" className="glass-strong px-5 py-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <StatusBadge status={job.status} />
          <p role="status" className="text-sm text-ink-soft">
            {job.status === "queued" ? "Waiting for a worker to pick this up." : `Evaluated ${fmtInt(job.processed_items)} of ${job.total_items ? fmtInt(job.total_items) : "an unknown number of"} residents.`}
          </p>
        </div>
        <ProgressBar done={job.processed_items} total={job.total_items} label="Dry run progress" tone="brand" />
        <p className="mt-2 text-xs text-ink-muted">Nothing is saved while this runs. You can leave this page and come back to the result.</p>
      </Card>
    );
  }
  if (job.status !== "completed" || !job.result) {
    return (
      <div role="alert" className="flex gap-3 rounded-card border border-danger-line bg-danger-bg px-5 py-4 text-danger">
        <Icon name="warning" className="mt-0.5 size-5" />
        <div>
          <p className="font-semibold">The dry run did not finish</p>
          <p className="mt-0.5">{displayText(job.error_summary) || `The run ended as ${humanize(job.status).toLowerCase()}.`}</p>
        </div>
      </div>
    );
  }
  return <Results job={job} r={job.result} currentHash={currentHash} />;
}

// ---- the section --------------------------------------------------------------------------------------------------------
export function DryRunSection({ guidelineId, versionId, versionNumber, hasRules, contentHash }: { guidelineId: string; versionId: string; versionNumber: number; hasRules: boolean; contentHash: string | null }) {
  const [sample, setSample] = useState<"ai_cohort" | "random_n">("ai_cohort");
  const [n, setN] = useState<number>(5000);
  const [district, setDistrict] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const act = useAction();
  const districts = useDistrictMap();
  const past = useApi(`studio:dry-runs:${versionId}`, (signal) => api<DryRunJob[]>(`/guidelines/${guidelineId}/versions/${versionId}/dry-runs`, { signal }));
  const latest = past.data?.data[0];
  const activeId = jobId ?? latest?.id ?? null;

  const run = async () => {
    const ok = await act.run(async () => {
      const body = { sample, n: sample === "random_n" ? n : undefined, district: district || undefined };
      const res = await apiPost<DryRunJob>(`/guidelines/${guidelineId}/versions/${versionId}/dry-run`, body);
      setJobId(res.data.id);
    });
    if (ok) past.reload();
  };

  return (
    <section aria-labelledby="studio-dry" className="space-y-4">
      <Card className="glass-strong">
        <CardHeader
          id="studio-dry"
          icon="flask"
          title="Run a dry run"
          subtitle={`Evaluate version ${versionNumber} on a sample and compare it with the approved version. Nothing is saved: no evaluation, case or message is created.`}
        />
        <div className="flex flex-wrap items-end gap-x-4 gap-y-3 px-5 pb-5">
          <FilterField label="Sample">
            <select aria-label="Sample" className={selectClass} value={sample} onChange={(e) => setSample(e.target.value as "ai_cohort" | "random_n")}>
              <option value="ai_cohort">AI cohort, 10,000 residents</option>
              <option value="random_n">Random sample of the population</option>
            </select>
          </FilterField>
          {sample === "random_n" && (
            <FilterField label="Sample size">
              <select aria-label="Sample size" className={selectClass} value={n} onChange={(e) => setN(Number(e.target.value))}>
                {SIZES.map((s) => (
                  <option key={s} value={s}>
                    {fmtInt(s)} residents
                  </option>
                ))}
              </select>
            </FilterField>
          )}
          <FilterField label="District">
            <select aria-label="District" className={selectClass} value={district} onChange={(e) => setDistrict(e.target.value)}>
              <option value="">All districts</option>
              {(districts.data ?? []).map((d) => (
                <option key={d.code} value={d.code}>
                  {d.district}
                </option>
              ))}
            </select>
          </FilterField>
          <GatedButton permission={[PERM.guidelinesApprove, PERM.processingExecute]} className={btnPrimary} onClick={run} disabled={act.pending || !hasRules}>
            <Icon name="zap" className="size-4" />
            {act.pending ? "Starting" : "Run dry run"}
          </GatedButton>
        </div>
        <p className="px-5 pb-4 text-xs text-ink-muted">Runs at about 100 residents a second, so 10,000 residents take about two minutes. A smaller sample answers in seconds.</p>
        {act.error && (
          <div className="px-5 pb-5">
            <ErrorPanel error={act.error} title="The dry run did not start" />
          </div>
        )}
      </Card>
      {activeId ? (
        <JobView key={activeId} guidelineId={guidelineId} versionId={versionId} id={activeId} currentHash={contentHash} />
      ) : past.loading ? (
        <div className="skeleton h-16" role="status" aria-label="Loading earlier dry runs" />
      ) : (
        <p className="rounded-control border border-dashed border-line px-5 py-4 text-ink-muted">No dry run has been run for this version yet. Choose a sample and run one to see who would become due.</p>
      )}
    </section>
  );
}
