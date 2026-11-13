"use client";

// The Preventive Care Journey (population level): five stage cards (identify, plan, engage, attend, complete), the
// journey funnel, cohort progress, daily activity and key insights, with tabs for cohorts, bottlenecks and the case list.
// Filters (programme, district) re-count the stages from real care gap cases. Every number opens a drawer with the real
// cases behind it. Route: /journey. Real data only: /analytics/funnel, /analytics/command-center, /analytics/engagement,
// /guidelines and /care-gaps. Reference figures with no endpoint (period change, median time to attend, age or sex cohorts)
// are not shown (see data/reports/ui-data-gaps.json). Purpose: show judges how the population moves through the loop.

import Link from "next/link";
import { useState } from "react";
import { apiList } from "@/lib/api";
import { fmtDate, fmtDayMonth, fmtInt, humanize } from "@/lib/format";
import type { CareGap } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, ColumnPairs, DataLabel, EmptyState, ErrorPanel, PageHeader, Pill, PriorityPill, btnSecondary, selectClass, Icon } from "@/components/ui";
import type { DuoName } from "@/components/ui";
import { CaseDrawer, GrowBar, InfoTip, STAGE_STATUS, StageArch, StageLink, countCases, type CaseQuery } from "./interactive";
import { PillTabs, TabPanel, useCommandCenter, useEngagementOverview, useFunnel, useProgrammes, type PillTab } from "./kit";
import type { CommandCenter, Funnel } from "./payloads";
import { fmtShare } from "./shared";

type Tab = "overview" | "cohorts" | "bottlenecks" | "cases";
const TABS: PillTab<Tab>[] = [
  { key: "overview", label: "Journey overview", icon: "layers" },
  { key: "cohorts", label: "Cohorts", icon: "users" },
  { key: "bottlenecks", label: "Bottlenecks", icon: "warning" },
  { key: "cases", label: "Case list", icon: "checklist" },
];

interface Stage {
  key: string;
  label: string;
  caption: string;
  tip: string;
  icon: DuoName;
  count: number | null;
  statuses: string[];
}

const META: { key: string; label: string; caption: string; tip: string; icon: DuoName; statuses: string[] }[] = [
  { key: "identify", label: "Identify", caption: "Eligible population", tip: "Residents whose latest guideline evaluation says they are due. Decided by versioned rules, not by AI.", icon: "eligible", statuses: STAGE_STATUS.eligible },
  { key: "plan", label: "Plan", caption: "Care plans created", tip: "Residents with a case that was planned: the capacity planner proposed a slot.", icon: "plan", statuses: STAGE_STATUS.planned },
  { key: "engage", label: "Engage", caption: "Residents reached", tip: "Residents with a case in outreach or beyond. Messages are simulated.", icon: "engage", statuses: STAGE_STATUS.contacted },
  { key: "attend", label: "Attend", caption: "Appointments attended", tip: "Cases whose appointment was attended. This stage counts appointments, not residents.", icon: "attend", statuses: STAGE_STATUS.attended },
  { key: "complete", label: "Complete", caption: "Care gaps closed", tip: "Cases closed as completed. A case closes only with verified completion evidence.", icon: "complete", statuses: STAGE_STATUS.completed },
];

const ratio = (a: number | null, b: number | null) => (a == null || b == null || b <= 0 ? null : a / b);

function stagesOf(f: Funnel | undefined, attended: number | undefined, filtered: Record<string, number> | undefined): Stage[] {
  const n = (k: string) => f?.stages.find((s) => s.key === k)?.count ?? null;
  const base = [n("due"), n("planned"), n("in_outreach"), attended ?? null, n("closed")];
  const keys = ["eligible", "planned", "contacted", "attended", "completed"];
  return META.map((m, i) => ({ ...m, count: filtered ? (filtered[keys[i]] ?? null) : base[i] }));
}

function StageCards({ stages, onOpen }: { stages: Stage[]; onOpen: (s: Stage) => void }) {
  return (
    <ol aria-label="Journey stages" className="grid gap-4 sm:grid-cols-2 lg:flex lg:items-end lg:gap-2">
      {stages.map((s, i) => {
        const prev = i > 0 ? stages[i - 1].count : null;
        const conv = ratio(s.count, prev);
        return (
          <li key={s.key} data-stage={s.key} className="flex min-w-0 flex-1 items-end gap-2">
            <div className="min-w-0 flex-1">
              <StageArch icon={s.icon} />
              <button
                type="button"
                onClick={() => onOpen(s)}
                className="glass-strong card -mt-3 block w-full rounded-card px-4 py-3 text-center transition-[box-shadow,transform] hover:-translate-y-0.5 hover:shadow-pop focus-visible:-translate-y-0.5"
              >
                <h3 className="font-serif text-lg font-semibold">{s.label}</h3>
                <p className="mt-1 flex flex-wrap items-center justify-center gap-2">
                  <span className="tnum text-2xl font-bold">{s.count == null ? "Not available" : fmtInt(s.count)}</span>
                  {conv != null && <Pill tone="brand">{fmtShare(conv)}</Pill>}
                </p>
                <p className="text-[0.8125rem] text-ink-muted">{s.caption}</p>
                <span className="sr-only">. Open the cases.</span>
              </button>
              <p className="mt-1 text-center">
                <InfoTip text={s.tip} label={`What ${s.label} means`} />
              </p>
            </div>
            {i < stages.length - 1 && <StageLink />}
          </li>
        );
      })}
    </ol>
  );
}

function JourneyFunnel({ stages, onOpen }: { stages: Stage[]; onOpen: (s: Stage) => void }) {
  const top = stages[0].count;
  return (
    <Card as="section" aria-labelledby="jf" className="glass-strong">
      <CardHeader id="jf" icon="filter" title="Journey funnel" subtitle="Share of the eligible population that reaches each stage. Select a bar to see its cases." aside={<DataLabel kind="live" />} />
      <ul aria-label="Journey funnel" className="space-y-2 px-5 pb-5">
        {stages.map((s) => {
          const r = ratio(s.count, top);
          return (
            <li key={s.key}>
              <button type="button" onClick={() => onOpen(s)} className="flex w-full items-center gap-3 rounded-control px-2 py-2 text-left hover:bg-brand-soft">
                <span className="w-20 shrink-0 font-medium">{s.label}</span>
                <span className="h-3 min-w-10 flex-1 overflow-hidden rounded-full bg-brand-soft" aria-hidden="true">
                  <GrowBar pct={Math.max(0.8, (r ?? 0) * 100)} />
                </span>
                <span className="tnum w-16 shrink-0 text-right font-semibold">{r == null ? "Not available" : fmtShare(r)}</span>
                <span className="sr-only">. Open the cases.</span>
              </button>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

const CLOSURE_TONE = (r: number | null) => (r == null ? "neutral" : r >= 0.5 ? "ok" : r >= 0.2 ? "warn" : "danger");

function Cohorts({ cc, limit, onOpen }: { cc: CommandCenter; limit?: number; onOpen: (title: string, q: CaseQuery) => void }) {
  const [by, setBy] = useState<"priority" | "district">("priority");
  const districts = [...(cc.equity?.districts ?? [])].sort((a, b) => b.cases - a.cases).slice(0, limit ?? 50);
  const cols = by === "priority" ? ["Cohort", "Cases", "Open", "Completed", "Closure"] : ["Cohort", "Cases", "Planned", "Completed", "Closure"];
  const maxCases = Math.max(1, ...cc.gaps_by_priority.items.map((p) => p.total), ...districts.map((d) => d.cases));
  return (
    <Card as="section" aria-labelledby="cp" className="glass-strong">
      <CardHeader id="cp" icon="users" title="Cohort progress" subtitle="Journey status by population group. Select a row to see its cases." />
      <div role="group" aria-label="Group cohorts by" className="flex gap-1 px-5 pb-2">
        {(["priority", "district"] as const).map((k) => (
          <button key={k} type="button" aria-pressed={by === k} onClick={() => setBy(k)} className={`${btnSecondary} ${by === k ? "bg-brand-soft text-brand-ink" : ""}`}>
            {k === "priority" ? "By priority" : "By district"}
          </button>
        ))}
      </div>
      <div role="region" aria-label="Cohort progress table" tabIndex={0} className="overflow-x-auto px-5 pb-5">
        {by === "district" && !cc.equity ? (
          <p className="text-ink-muted">Your role cannot view district figures.</p>
        ) : (
          <table className="w-full min-w-[26rem] border-collapse text-left text-[0.8125rem]">
            <caption className="sr-only">Cohort progress grouped by {by}</caption>
            <thead>
              <tr className="border-b border-line text-ink-muted">
                {cols.map((c, i) => (
                  <th key={c} scope="col" className={`px-2 py-2 font-medium ${i ? "text-right" : ""}`}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {by === "priority"
                ? cc.gaps_by_priority.items.map((p) => {
                    const r = ratio(p.completed, p.total);
                    const name = `${p.priority === "elevated" ? "Medium" : humanize(p.priority)} priority`;
                    return (
                      <tr key={p.priority} className="tnum border-b border-line last:border-0">
                        <th scope="row" className="px-2 py-2.5 text-left font-medium">
                          <button type="button" onClick={() => onOpen(name, { priority: p.priority })} className="flex w-full flex-col gap-1 text-left">
                            <span className="inline-flex items-center gap-1.5">
                              <PriorityPill priority={p.priority} /> {name}
                            </span>
                            <span className="block h-1.5 overflow-hidden rounded-full bg-brand-soft" aria-hidden="true">
                              <GrowBar pct={(p.total / maxCases) * 100} />
                            </span>
                            <span className="sr-only">. Open the cases.</span>
                          </button>
                        </th>
                        <td className="px-2 py-2.5 text-right">{fmtInt(p.total)}</td>
                        <td className="px-2 py-2.5 text-right">{fmtInt(p.open)}</td>
                        <td className="px-2 py-2.5 text-right">{fmtInt(p.completed)}</td>
                        <td className="px-2 py-2.5 text-right">
                          <Pill tone={CLOSURE_TONE(r)}>{fmtShare(r)}</Pill>
                        </td>
                      </tr>
                    );
                  })
                : districts.map((d) => (
                    <tr key={d.district_code} className="tnum border-b border-line last:border-0">
                      <th scope="row" className="px-2 py-2.5 text-left font-medium">
                        <button type="button" onClick={() => onOpen(`District ${d.district_code}`, { district: d.district_code })} className="flex w-full flex-col gap-1 text-left">
                          <span className="inline-flex items-center gap-1.5">
                            <Icon name="pin" className="size-4 text-brand-ink" /> District {d.district_code}
                          </span>
                          <span className="block h-1.5 overflow-hidden rounded-full bg-brand-soft" aria-hidden="true">
                            <GrowBar pct={(d.cases / maxCases) * 100} />
                          </span>
                          <span className="sr-only">. Open the cases.</span>
                        </button>
                      </th>
                      <td className="px-2 py-2.5 text-right">{fmtInt(d.cases)}</td>
                      <td className="px-2 py-2.5 text-right">{fmtInt(d.planned)}</td>
                      <td className="px-2 py-2.5 text-right">{fmtInt(d.completed)}</td>
                      <td className="px-2 py-2.5 text-right">
                        <Pill tone={CLOSURE_TONE(d.closure_rate)}>{fmtShare(d.closure_rate)}</Pill>
                      </td>
                    </tr>
                  ))}
            </tbody>
          </table>
        )}
      </div>
    </Card>
  );
}

function Insights({ stages, cc, onOpen }: { stages: Stage[]; cc: CommandCenter | undefined; onOpen: (s: Stage) => void }) {
  const closure = cc?.kpis.find((k) => k.key === "closure_rate");
  let worst: { from: Stage; to: Stage; r: number } | null = null;
  for (let i = 1; i < stages.length; i++) {
    const r = ratio(stages[i].count, stages[i - 1].count);
    if (r != null && (worst == null || r < worst.r)) worst = { from: stages[i - 1], to: stages[i], r };
  }
  const alert = cc?.alerts[0];
  return (
    <Card as="section" aria-labelledby="ki" className="glass-strong">
      <CardHeader id="ki" icon="sparkles" title="Key insights" aside={<DataLabel kind="live" />} />
      <ul className="divide-y divide-line px-5 pb-4">
        <li className="flex items-start gap-3 py-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-full bg-tint-green text-icon-green">
            <Icon name="activity" className="size-5" />
          </span>
          <div>
            <p className="font-semibold">{closure?.value == null ? "Closure rate not available" : `${fmtShare(closure.value)} of care gaps closed`}</p>
            <p className="text-ink-muted">{closure?.numerator != null ? `${fmtInt(closure.numerator)} of ${fmtInt(closure.denominator)} cases.` : "No cases yet."}</p>
          </div>
        </li>
        <li className="flex items-start gap-3 py-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-full bg-tint-amber text-icon-amber">
            <Icon name="warning" className="size-5" />
          </span>
          <div>
            <p className="font-semibold">{worst ? `${worst.to.label} is the main drop off` : "No drop off to report"}</p>
            <p className="text-ink-muted">{worst ? `Only ${fmtShare(worst.r)} of ${worst.from.caption.toLowerCase()} reach the next stage (${fmtInt(worst.to.count)} of ${fmtInt(worst.from.count)}).` : "The stages have no counts yet."}</p>
            {worst && (
              <button type="button" onClick={() => onOpen(worst.from)} className="mt-1 text-brand-ink underline underline-offset-2">
                See the cases at {worst.from.label}
              </button>
            )}
          </div>
        </li>
        <li className="flex items-start gap-3 py-3">
          <span className={`grid size-10 shrink-0 place-items-center rounded-full ${alert ? "bg-tint-rose text-icon-rose" : "bg-tint-blue text-icon-blue"}`}>
            <Icon name={alert ? "alert" : "info"} className="size-5" />
          </span>
          <div>
            <p className="font-semibold">{alert ? alert.title : "No active alerts"}</p>
            <p className="text-ink-muted">{alert ? alert.message : "Capacity, outreach backlog and review queue are within their limits."}</p>
          </div>
        </li>
      </ul>
    </Card>
  );
}

function Activity({ cc }: { cc: CommandCenter }) {
  const pts = cc.closure_trend.points.slice(-14);
  const total = pts.reduce((a, p) => a + p.opened + p.closed, 0);
  return (
    <Card as="section" aria-labelledby="da" className="glass-strong">
      <CardHeader id="da" icon="chart" title="Daily activity" subtitle={`Cases opened and cases closed per day over the last ${pts.length} days with activity.`} aside={<DataLabel kind="live" />} />
      <div className="px-5 pb-5">
        {total === 0 ? (
          <p className="text-ink-muted">No case was opened or closed in this window.</p>
        ) : (
          <ColumnPairs groups={pts.map((p) => ({ label: fmtDayMonth(p.date), a: p.opened, b: p.closed }))} aLabel="Opened" bLabel="Closed" height={200} ariaLabel="Cases opened and closed per day" tableCaption="Cases opened and closed per day" />
        )}
      </div>
    </Card>
  );
}

function Bottlenecks({ stages, f, cc, onOpen }: { stages: Stage[]; f: Funnel | undefined; cc: CommandCenter | undefined; onOpen: (s: Stage) => void }) {
  return (
    <div className="grid gap-5 lg:grid-cols-2 *:min-w-0">
      <Card as="section" aria-labelledby="bn" className="glass-strong">
        <CardHeader id="bn" icon="filter" title="Where residents drop off" subtitle="Each row compares a stage with the stage before it. Select a stage name to see its cases." />
        <div role="region" aria-label="Stage conversion table" tabIndex={0} className="overflow-x-auto px-5 pb-5">
          <table className="w-full min-w-[26rem] border-collapse text-left">
            <caption className="sr-only">Stage to stage conversion</caption>
            <thead>
              <tr className="border-b border-line text-ink-muted">
                <th scope="col" className="px-2 py-2 font-medium">Stage</th>
                <th scope="col" className="px-2 py-2 text-right font-medium">Count</th>
                <th scope="col" className="px-2 py-2 text-right font-medium">Kept from stage before</th>
                <th scope="col" className="px-2 py-2 text-right font-medium">Lost</th>
              </tr>
            </thead>
            <tbody>
              {stages.map((s, i) => {
                const prev = i > 0 ? stages[i - 1].count : null;
                const r = ratio(s.count, prev);
                return (
                  <tr key={s.key} className="tnum border-b border-line last:border-0">
                    <th scope="row" className="px-2 py-2.5 text-left font-medium">
                      <button type="button" onClick={() => onOpen(s)} className="text-brand-ink underline underline-offset-2">{s.label}</button>
                    </th>
                    <td className="px-2 py-2.5 text-right">{s.count == null ? "Not available" : fmtInt(s.count)}</td>
                    <td className="px-2 py-2.5 text-right">{i === 0 ? "Start" : fmtShare(r)}</td>
                    <td className="px-2 py-2.5 text-right">{i === 0 || prev == null || s.count == null ? "Not available" : prev - s.count > 0 ? fmtInt(prev - s.count) : "None"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
      <div className="space-y-5">
        <Card as="section" aria-labelledby="lk" className="glass-strong">
          <CardHeader id="lk" icon="ban" title="Left the journey without completion" subtitle="Cases, not residents." />
          <ul className="space-y-1.5 px-5 pb-5">
            {(f?.leakage.items ?? []).map((l) => (
              <li key={l.status} className="flex justify-between gap-3">
                <span>{humanize(l.status)}</span>
                <span className="tnum font-semibold">{fmtInt(l.cases)}</span>
              </li>
            ))}
          </ul>
        </Card>
        <Card as="section" aria-labelledby="al" className="glass-strong">
          <CardHeader id="al" icon="alert" title="Checks on the journey" subtitle="Each rule shows whether it is raising an alert." />
          <ul className="space-y-2 px-5 pb-5">
            {(cc?.alert_rules ?? []).map((a) => (
              <li key={a.key} className="flex items-start gap-2">
                <Pill tone={a.status === "alert" ? "danger" : "ok"} icon={a.status === "alert" ? "alert" : "check-circle"}>{a.status === "alert" ? "Alert" : "Clear"}</Pill>
                <span>
                  <span className="font-medium">{a.title}.</span> <span className="text-ink-muted">{a.message}</span>
                </span>
              </li>
            ))}
            {(cc?.alert_rules.length ?? 0) === 0 && <li className="text-ink-muted">No alert rules were returned.</li>}
          </ul>
        </Card>
      </div>
    </div>
  );
}

function CaseList({ query }: { query: CaseQuery }) {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const res = useApi(`journey-cases:${page}:${status}:${query.care_activity ?? ""}:${query.district ?? ""}`, (s) => apiList<CareGap>("/care-gaps", { page, page_size: 15, status: status || undefined, care_activity: query.care_activity, district: query.district }, s));
  const cols: Column<CareGap>[] = [
    { key: "a", header: "Care activity", cell: (c) => <span className="font-medium">{humanize(c.care_activity)}</span> },
    { key: "p", header: "Priority", cell: (c) => <PriorityPill priority={c.priority} /> },
    { key: "s", header: "Status", cell: (c) => <StatusBadge status={c.status} /> },
    { key: "o", header: "Opened", cell: (c) => fmtDate(c.opened_at) },
    { key: "j", header: "Journey", cell: (c) => <Link href={`/residents/${c.resident_id}/journey`} className="inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">Open journey <Icon name="arrow-right" className="size-4" /></Link> },
  ];
  const meta = res.data?.meta;
  return (
    <Card as="section" aria-labelledby="cl" className="glass-strong">
      <CardHeader
        id="cl"
        icon="checklist"
        title="Case list"
        subtitle="Open any case to see that resident's whole journey."
        aside={
          <>
            <label htmlFor="cl-status" className="font-medium">Status</label>
            <select id="cl-status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className={`${selectClass} min-h-9 py-0`}>
              <option value="">All statuses</option>
              {["identified", "approved_for_planning", "planned", "outreach_pending", "outreach_completed", "appointment_scheduled", "awaiting_completion_evidence", "completed", "closed_not_completed", "escalated"].map((s) => (
                <option key={s} value={s}>{humanize(s)}</option>
              ))}
            </select>
          </>
        }
      />
      <div className="px-5 pb-5">
        <DataTable caption="Care gap cases" columns={cols} rows={res.data?.data ?? null} getRowId={(c) => c.id} loading={res.loading} error={res.error} onRetry={res.reload} emptyTitle="No cases match this filter" page={meta?.page ?? page} pageSize={meta?.page_size ?? 15} total={meta?.total} totalPages={meta?.total_pages} onPageChange={setPage} />
      </div>
    </Card>
  );
}

export function PopulationJourney() {
  const [tab, setTab] = useState<Tab>("overview");
  const [programme, setProgramme] = useState("");
  const [district, setDistrict] = useState("");
  const [drawer, setDrawer] = useState<{ title: string; subtitle?: string; query: CaseQuery } | null>(null);
  const funnel = useFunnel();
  const cc = useCommandCenter();
  const eng = useEngagementOverview();
  const programmes = useProgrammes();
  const filtered = !!(programme || district);
  const counts = useApi(`journey-counts:${programme}:${district}`, async (s) => {
    if (!filtered) return null;
    const base: CaseQuery = { care_activity: programme || undefined, district: district || undefined };
    const entries = await Promise.all(Object.entries(STAGE_STATUS).map(async ([k, st]) => [k, await countCases({ ...base, status: st }, s)] as const));
    return Object.fromEntries(entries) as Record<string, number>;
  });
  const f = funnel.data?.data;
  const c = cc.data?.data;
  const stages = stagesOf(f, eng.data?.data.funnel.cases_attended, filtered ? (counts.data ?? undefined) : undefined);
  const activities = (programmes.data?.data.programmes ?? []).map((p) => p.care_activity);
  const districts = (c?.equity?.districts ?? []).map((d) => d.district_code).sort();
  const base: CaseQuery = { care_activity: programme || undefined, district: district || undefined };
  const open = (title: string, q: CaseQuery) => setDrawer({ title, subtitle: filtered ? `Filtered by ${[programme && humanize(programme), district && `district ${district}`].filter(Boolean).join(" and ")}` : undefined, query: { ...base, ...q } });
  const openStage = (s: Stage) => open(`${s.label}: ${s.caption.toLowerCase()}`, { status: s.statuses });
  const loading = !f && !funnel.error;

  return (
    <>
      <PageHeader title="The preventive care journey" description="Track how the population moves from identification to care completion." actions={<PillTabs tabs={TABS} active={tab} onChange={setTab} label="Journey views" panelId="journey-panel" />} />
      <div className="mb-5 flex flex-wrap items-end gap-3" role="group" aria-label="Journey filters">
        <div>
          <label htmlFor="jf-prog" className="mb-1 block font-medium">Programme</label>
          <select id="jf-prog" value={programme} onChange={(e) => setProgramme(e.target.value)} className={`${selectClass} min-w-52`}>
            <option value="">All programmes</option>
            {activities.map((a) => <option key={a} value={a}>{humanize(a)}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="jf-dist" className="mb-1 block font-medium">District</label>
          <select id="jf-dist" value={district} onChange={(e) => setDistrict(e.target.value)} className={`${selectClass} min-w-40`} disabled={districts.length === 0}>
            <option value="">All districts</option>
            {districts.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        {filtered && (
          <button type="button" className={btnSecondary} onClick={() => { setProgramme(""); setDistrict(""); }}>
            Clear filters
          </button>
        )}
        <p className="pb-2 text-[0.8125rem] text-ink-muted">{filtered ? "Counts are cases for this filter." : "Counts are residents, except Attend, which counts appointments."}</p>
      </div>
      {funnel.error ? (
        <ErrorPanel error={funnel.error} onRetry={funnel.reload} title="Could not load the journey" />
      ) : loading ? (
        <div role="status" aria-label="Loading the journey" className="space-y-4">
          <div className="skeleton h-56 w-full" />
          <div className="skeleton h-64 w-full" />
        </div>
      ) : (
        <TabPanel id="journey-panel" label={TABS.find((t) => t.key === tab)!.label}>
          {tab === "overview" && (
            <div className="space-y-6">
              <StageCards stages={stages} onOpen={openStage} />
              <div className="grid gap-5 lg:grid-cols-[1fr_1.15fr_0.85fr] *:min-w-0">
                <JourneyFunnel stages={stages} onOpen={openStage} />
                {c ? <Cohorts cc={c} limit={6} onOpen={open} /> : cc.error ? <ErrorPanel error={cc.error} onRetry={cc.reload} title="Could not load cohorts" /> : <div className="skeleton h-64" />}
                <Insights stages={stages} cc={c} onOpen={openStage} />
              </div>
              {c && <Activity cc={c} />}
            </div>
          )}
          {tab === "cohorts" && (c ? <Cohorts cc={c} onOpen={open} /> : cc.error ? <ErrorPanel error={cc.error} onRetry={cc.reload} /> : <EmptyState title="Loading cohorts" />)}
          {tab === "bottlenecks" && <Bottlenecks stages={stages} f={f} cc={c} onOpen={openStage} />}
          {tab === "cases" && <CaseList query={base} />}
        </TabPanel>
      )}
      <CaseDrawer open={drawer != null} onClose={() => setDrawer(null)} title={drawer?.title ?? ""} subtitle={drawer?.subtitle} query={drawer?.query ?? {}} />
    </>
  );
}
