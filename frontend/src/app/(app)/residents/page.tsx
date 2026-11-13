"use client";

import Link from "next/link";
import { Suspense, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, apiList, isMissingEndpoint } from "@/lib/api";
import { LANGUAGES, ageBandLabel, fmtDate, fmtInt, fmtPct, humanize } from "@/lib/format";
import type { CareGap, CommandCenter, Family, Resident, ResidentDetail, ResidentProfile, ScopeMeta, TimelineEntry } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { displayText } from "@/lib/display";
import { StatusBadge } from "@/components/status-badge";
import { Timeline, type TimelineItem } from "@/components/timeline";
import { BarList, Card, CardHeader, ErrorPanel, FilterField, Icon, PageHeader, Pill, PillTabs, PriorityPill, ProgressBar, Unavailable, btnPrimary, btnSecondary, inputClass, selectClass } from "@/components/ui";
import { EvidenceBlock, FamilyBlock, Records, TimelineBlock } from "./[id]/resident-detail";

const PAGE_SIZE = 12;
const AGE_BANDS = ["0-17", "18-29", "30-39", "40-49", "50-59", "60-69", "70-79", "80+"];
const EMPTY = { district: "", sex: "", language: "", age_band: "" };
type Tab = "overview" | "journey" | "family" | "records";
const TABS = [
  { id: "overview", label: "Overview" },
  { id: "journey", label: "Care journey" },
  { id: "family", label: "Family" },
  { id: "records", label: "Records and evidence" },
] as const satisfies readonly { id: Tab; label: string }[];

const shortId = (id: string) => id.slice(0, 8);
const initials = (id: string) => id.replace(/-/g, "").slice(0, 2).toUpperCase();

/** Everything on the right of the list: profile, tabs and the compact care journey. Keyed by resident id. */
function ResidentPanel({ id }: { id: string }) {
  const [tab, setTab] = useState<Tab>("overview");
  const res = useApi(`resident:${id}`, (signal) => api<ResidentDetail>(`/residents/${id}`, { signal }));
  const gaps = useApi(`resident-gaps:${id}`, (signal) => apiList<CareGap>("/care-gaps", { resident_id: id, page_size: 50 }, signal));
  const prof = useApi(`profile:${id}`, (signal) => api<ResidentProfile>(`/residents/${id}/profile`, { signal }));
  const fam = useApi(`family:${id}`, (signal) => api<Family>(`/residents/${id}/family`, { signal }));
  const tl = useApi(`timeline-mini:${id}`, (signal) => apiList<TimelineEntry>(`/residents/${id}/timeline`, { page_size: 6 }, signal));
  const r = res.data?.data;

  if (res.error) return <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load this resident" />;
  if (!r) return <div className="skeleton h-96 w-full" role="status" aria-label="Loading resident" />;

  const caseRows = gaps.data?.data ?? [];
  const closed = caseRows.filter((g) => ["completed", "closed"].includes(g.status)).length;
  const open = caseRows.filter((g) => !["completed", "closed", "cancelled", "declined"].includes(g.status));
  const counts = Object.entries(r.record_counts).map(([category, count]) => ({ label: humanize(category), value: count, display: fmtInt(count) }));
  const items: TimelineItem[] = (tl.data?.data ?? []).map((e) => ({
    id: e.event_id,
    at: e.occurred_at,
    tone: e.source === "document" ? "info" : "neutral",
    title: (
      <>
        {humanize(e.kind)}
        {e.title && e.title !== e.kind ? `: ${displayText(e.title)}` : ""}
      </>
    ),
    detail: e.source === "document" ? "Registered document" : "Clinical record",
  }));

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_18rem] [&>*]:min-w-0">
      <div className="space-y-4">
        <Card as="section" aria-labelledby="rx-profile">
          <div className="flex flex-wrap items-center gap-5 px-5 pt-5">
            <span aria-hidden="true" className="grad-brand grid size-20 shrink-0 place-items-center rounded-full font-display text-2xl font-bold shadow-[0_10px_20px_-10px_rgb(79_51_168/0.8)]">
              {initials(r.id)}
            </span>
            <div className="min-w-0 flex-1">
              <h2 id="rx-profile" className="font-display text-2xl leading-8 font-bold">Resident {shortId(r.id)}</h2>
              <p className="text-ink-soft">
                {r.age == null ? "Age not recorded" : `${r.age} years`}
                <span className="px-2 text-line-strong">|</span>
                {humanize(r.administrative_sex)}
                <span className="px-2 text-line-strong">|</span>
                {r.preferred_language ? (LANGUAGES[r.preferred_language] ?? r.preferred_language) : "Language not recorded"}
              </p>
              <div className="mt-2.5 flex flex-wrap gap-2">
                <Pill tone="neutral" icon="pin">{prof.data?.data.place ? `${prof.data.data.place.city}, ${prof.data.data.place.state}` : (r.district_code ?? "No district")}</Pill>
                <Pill tone="neutral" icon="users">Family links: {fmtInt(r.family_edge_count)}</Pill>
                <Pill tone="neutral" icon="file">Documents: {fmtInt(r.document_count)}</Pill>
              </div>
            </div>
            <Link href={`/residents/${r.id}`} className={btnSecondary}>
              Open full record
              <Icon name="arrow-right" className="size-4" />
            </Link>
          </div>
          <div className="px-5 py-4">
            <PillTabs idBase="rx" label="Resident views" items={TABS} value={tab} onChange={setTab} />
          </div>
        </Card>

        <div role="tabpanel" id="rx-panel" aria-labelledby={`rx-tab-${tab}`} className="space-y-4 [&>*]:min-w-0">
          {tab === "overview" && (
            <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
              <Card as="section" aria-labelledby="rx-gaps">
                <CardHeader id="rx-gaps" icon="target" title={`Active care gaps (${open.length})`} />
                <div className="px-5 pb-5">
                  {gaps.error ? (
                    <p className="py-6 text-center text-ink-muted">Care gaps are not available for your role.</p>
                  ) : !gaps.data ? (
                    <div className="skeleton h-24 w-full" role="status" aria-label="Loading care gaps" />
                  ) : open.length ? (
                    <ul className="divide-y divide-line">
                      {open.map((g) => (
                        <li key={g.id} className="flex flex-wrap items-center gap-2 py-2.5">
                          <Link href={`/care-gaps/${g.id}`} className="min-w-0 flex-1 font-medium text-brand-ink underline underline-offset-2">
                            {humanize(g.care_activity)}
                          </Link>
                          <PriorityPill priority={g.priority} />
                          <StatusBadge status={g.status} />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="py-6 text-center text-ink-muted">No open care gaps for this resident.</p>
                  )}
                </div>
              </Card>
              <Card as="section" aria-labelledby="rx-records">
                <CardHeader id="rx-records" icon="clipboard" title="Records by category" />
                <div className="px-5 pb-5">
                  {counts.length ? (
                    <BarList ariaLabel="Clinical records by category" tableCaption="Clinical records by category" valueHeader="Records" labelWidth="w-28" rows={counts} />
                  ) : (
                    <p className="py-6 text-center text-ink-muted">No clinical records loaded.</p>
                  )}
                </div>
              </Card>
              <Card as="section" aria-labelledby="rx-coverage" className="lg:col-span-2">
                <CardHeader id="rx-coverage" icon="shield" title="Preventive care coverage" subtitle="One row per program, from the latest guideline evaluation" />
                <div className="px-5 pb-5">
                  {prof.error ? (
                    <p className="py-4 text-ink-muted">Program coverage is not available for your role.</p>
                  ) : !prof.data ? (
                    <div className="skeleton h-16 w-full" role="status" aria-label="Loading coverage" />
                  ) : prof.data.data.coverage?.programmes.length ? (
                    <ul className="divide-y divide-line">
                      {prof.data.data.coverage.programmes.map((p) => (
                        <li key={p.care_activity} className="flex flex-wrap items-center gap-2 py-2.5">
                          <span className="min-w-0 flex-1 font-medium">{p.label}</span>
                          <Pill tone={p.status === "due" ? "warn" : p.status === "up_to_date" ? "ok" : "neutral"}>{p.status_label}</Pill>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="py-4 text-ink-muted">No program has been evaluated for this resident.</p>
                  )}
                </div>
              </Card>
              <Card as="section" aria-labelledby="rx-family" className="lg:col-span-2">
                <CardHeader id="rx-family" icon="users" title="Family" subtitle="Relatives linked to this resident" aside={<Link href={`/residents/${r.id}/journey`} className="text-[0.8125rem] font-semibold text-brand-ink underline underline-offset-2">Open pedigree and journey</Link>} />
                <div className="px-5 pb-5">
                  {fam.error ? (
                    <p className="py-4 text-ink-muted">Family links are not available for your role.</p>
                  ) : !fam.data ? (
                    <div className="skeleton h-12 w-full" role="status" aria-label="Loading family" />
                  ) : fam.data.data.edges.length ? (
                    <ul className="flex flex-wrap gap-2">
                      {fam.data.data.edges.map((e) => (
                        <li key={e.id}>
                          <Pill tone="brand" icon="user">{humanize(e.relationship_type)}</Pill>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="py-4 text-ink-muted">No family links recorded.</p>
                  )}
                </div>
              </Card>
            </div>
          )}
          {tab === "journey" && <TimelineBlock id={id} />}
          {tab === "family" && <FamilyBlock id={id} />}
          {tab === "records" && (
            <>
              <EvidenceBlock id={id} />
              <Records id={id} />
            </>
          )}
        </div>
      </div>

      <Card as="section" aria-labelledby="rx-journey" className="self-start">
        <CardHeader id="rx-journey" title="Care journey" aside={<Link href={`/residents/${r.id}/journey`} className="text-[0.8125rem] font-semibold text-brand-ink underline underline-offset-2">View full journey</Link>} />
        <div className="px-5 pb-5">
          {tl.error ? (
            <p className="py-4 text-ink-muted">The timeline is not available for your role.</p>
          ) : !tl.data ? (
            <div className="skeleton h-40 w-full" role="status" aria-label="Loading timeline" />
          ) : items.length ? (
            <Timeline label="Latest resident events" items={items} />
          ) : (
            <p className="py-4 text-ink-muted">No timeline entries.</p>
          )}
          <div className="tile mt-5 p-3.5">
            <p className="text-[0.8125rem] font-semibold text-ink-soft">Care gap progress</p>
            {caseRows.length ? <ProgressBar done={closed} total={caseRows.length} label="Care gaps completed" tone="brand" /> : <p className="text-xs text-ink-muted">No care gaps raised.</p>}
            <p className="mt-1 text-xs text-ink-muted">Born {fmtDate(r.birth_date)}, {ageBandLabel(r.age_band)}</p>
          </div>
        </div>
      </Card>
    </div>
  );
}

function Explorer() {
  const router = useRouter();
  const params = useSearchParams();
  const urlSearch = params.get("search") ?? "";
  const urlDistrict = params.get("district") ?? "";
  const [page, setPage] = useState(1);
  const [typed, setFilters] = useState(EMPTY);
  const filters = { ...typed, district: typed.district || urlDistrict };
  const [picked, setPicked] = useState<string | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const list = useApi(`residents:${page}:${urlSearch}:${JSON.stringify(filters)}`, (signal) => apiList<Resident>("/residents", { page, page_size: PAGE_SIZE, search: urlSearch, ...filters }, signal));
  const scope = useApi("residents-scope", (signal) => api<ScopeMeta>("/meta/scope", { signal }));
  const scopeNames = new Map((scope.data?.data.districts ?? []).map((d) => [d.code, d.name]));
  const kpis = useApi("residents-kpis", (signal) => api<CommandCenter>("/analytics/command-center", { signal }));
  const meta = list.data?.meta;
  const rows = list.data?.data ?? null;
  const selected = picked ?? rows?.[0]?.id ?? null;
  const filtered = urlSearch !== "" || Object.values(filters).some(Boolean);
  const pages = Math.max(1, meta?.total_pages ?? 1);
  const cc = kpis.data?.data;
  const kp = (k: string) => cc?.kpis.find((x) => x.key === k);

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const v = (k: string) => String(f.get(k) ?? "").trim();
    if (v("search") && v("search").length < 2) {
      setSearchError("Enter at least 2 characters of a resident id or source id.");
      return;
    }
    setSearchError(null);
    setFilters({ district: v("district"), sex: v("sex"), language: v("language"), age_band: v("age_band") });
    setPage(1);
    setPicked(null);
    router.replace(v("search") ? `/residents?search=${encodeURIComponent(v("search"))}` : "/residents");
  }

  return (
    <>
      <PageHeader
        crumbs={["Residents", "Resident Explorer"]}
        title="Resident Explorer"
        description="Search, explore and review resident preventive care and care journeys."
        actions={
          <div className="glass grid grid-cols-2 gap-x-6 gap-y-2 px-5 py-3 sm:grid-cols-4 [&>*]:min-w-0">
            {[
              ["Total residents", meta?.total ?? kp("residents_total")?.value],
              ["With open care gaps", kp("residents_with_open_gaps")?.value],
              ["Open care gaps", kp("care_gaps_open")?.value],
              ["Closure rate", null],
            ]
              .filter(([k]) => k !== "Closure rate" || kp("closure_rate"))
              .map(([k, v]) => (
                <div key={k as string}>
                  <p className="tnum font-display text-xl leading-6 font-bold">{k === "Closure rate" ? fmtPct(kp("closure_rate")?.value) : fmtInt(v as number | null | undefined)}</p>
                  <p className="text-xs text-ink-muted">{k as string}</p>
                </div>
              ))}
          </div>
        }
      />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] [&>*]:min-w-0">
        <Card as="section" aria-labelledby="rx-list" className="self-start">
          <CardHeader id="rx-list" title="Residents" aside={<Pill tone="neutral">{fmtInt(meta?.total)}</Pill>} />
          <form role="search" aria-label="Find residents" onSubmit={onSubmit} className="space-y-3 px-5 pb-4">
            <div>
              <label htmlFor="resident-search" className="sr-only">
                Search
              </label>
              <input id="resident-search" key={urlSearch} name="search" type="search" defaultValue={urlSearch} className={inputClass} placeholder="Resident or source id" aria-invalid={!!searchError} aria-describedby={searchError ? "resident-search-err" : undefined} />
              {searchError && (
                <p id="resident-search-err" role="alert" className="mt-1 text-danger">
                  {searchError}
                </p>
              )}
            </div>
            <details className="text-[0.8125rem]">
              <summary className="cursor-pointer font-semibold text-brand-ink">Filters</summary>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <FilterField label="District">
                  <select name="district" className={selectClass} defaultValue={filters.district}>
                    <option value="">Any</option>
                    {(scope.data?.data.districts ?? []).map((d) => (
                      <option key={d.code} value={d.code}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </FilterField>
                <FilterField label="Sex">
                  <select name="sex" className={selectClass} defaultValue={filters.sex}>
                    <option value="">Any</option>
                    {["female", "male", "other", "unknown"].map((s) => (
                      <option key={s} value={s}>
                        {humanize(s)}
                      </option>
                    ))}
                  </select>
                </FilterField>
                <FilterField label="Language">
                  <select name="language" className={selectClass} defaultValue={filters.language}>
                    <option value="">Any</option>
                    {Object.entries(LANGUAGES).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v}
                      </option>
                    ))}
                  </select>
                </FilterField>
                <FilterField label="Age band">
                  <select name="age_band" className={selectClass} defaultValue={filters.age_band}>
                    <option value="">Any</option>
                    {AGE_BANDS.map((b) => (
                      <option key={b} value={b}>
                        {ageBandLabel(b)}
                      </option>
                    ))}
                  </select>
                </FilterField>
              </div>
            </details>
            <div className="flex gap-2">
              <button type="submit" className={`${btnPrimary} flex-1`}>
                Search
              </button>
              <button
                type="reset"
                className={btnSecondary}
                onClick={() => {
                  setFilters(EMPTY);
                  setSearchError(null);
                  setPage(1);
                  setPicked(null);
                  router.replace("/residents");
                }}
              >
                Clear
              </button>
            </div>
          </form>

          {list.error ? (
            <div className="px-5 pb-5">
              {isMissingEndpoint(list.error) ? <Unavailable endpoint="GET /api/v1/residents" /> : <ErrorPanel error={list.error} onRetry={list.reload} />}
            </div>
          ) : !rows ? (
            <div className="space-y-2 px-5 pb-5" role="status" aria-label="Loading residents">
              {Array.from({ length: 6 }, (_, i) => (
                <div key={i} className="skeleton h-14 w-full" />
              ))}
            </div>
          ) : rows.length === 0 ? (
            <p className="px-5 pb-5 text-ink-muted">{filtered ? "No residents match these filters. Change or clear the filters." : "No residents have been loaded yet."}</p>
          ) : (
            <ul aria-label="Residents" className="divide-y divide-line px-2 pb-1">
              {rows.map((r) => {
                const on = r.id === selected;
                return (
                  <li key={r.id}>
                    <button
                      type="button"
                      aria-current={on ? "true" : undefined}
                      onClick={() => setPicked(r.id)}
                      className={`flex w-full items-center gap-3 rounded-control px-3 py-2.5 text-left ${on ? "bg-brand-soft shadow-[inset_0_0_0_1px_rgb(112_84_196/0.25)]" : "hover:bg-white/70"}`}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block font-semibold">
                          {r.age == null ? "Age not recorded" : `Age ${r.age}`}, {humanize(r.administrative_sex)}
                        </span>
                        <span className="block truncate text-xs text-ink-muted">
                          {scopeNames.get(r.district_code ?? "") ?? r.district_code ?? "No district"}, resident {shortId(r.id)}
                        </span>
                      </span>
                      <Icon name="chevron-right" className="size-4 text-ink-muted" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {rows && (
            <div className="flex items-center justify-between gap-2 border-t border-line px-5 py-3">
              <p role="status" className="tnum text-xs text-ink-muted">
                Page {fmtInt(meta?.page ?? page)} of {fmtInt(pages)}
              </p>
              <div className="flex gap-2">
                <button type="button" className={btnSecondary} disabled={list.loading || page <= 1} onClick={() => { setPage(page - 1); setPicked(null); }} aria-label="Previous page">
                  <Icon name="chevron-left" className="size-4" />
                </button>
                <button type="button" className={btnSecondary} disabled={list.loading || page >= pages} onClick={() => { setPage(page + 1); setPicked(null); }} aria-label="Next page">
                  <Icon name="chevron-right" className="size-4" />
                </button>
              </div>
            </div>
          )}
        </Card>

        {selected ? <ResidentPanel key={selected} id={selected} /> : <Card className="grid min-h-64 place-items-center p-8 text-ink-muted">{list.loading ? "Loading" : "Select a resident to see their profile."}</Card>}
      </div>
    </>
  );
}

export default function ResidentsPage() {
  return (
    <Suspense fallback={<p role="status" className="text-ink-muted">Loading residents…</p>}>
      <Explorer />
    </Suspense>
  );
}
