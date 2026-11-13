"use client";

// Preventive care programs: the active programme with its live counts, the next best actions computed from open cases,
// and every programme (a guideline care activity with an approved version) with its case counts. Real data only:
// GET /guidelines and GET /care-gaps counts (page_size=1, meta.total). No programme is shown that has no approved guideline.
// Route: /programs. Purpose: show judges the programmes the system runs and what needs doing next.

import Link from "next/link";
import { useState } from "react";
import { apiList } from "@/lib/api";
import { fmtInt, humanize } from "@/lib/format";
import type { Guideline } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { Card, CardHeader, DataLabel, ErrorPanel, PageHeader, Pill, GlassTile, type DuoName, Icon } from "@/components/ui";
import { CaseDrawer, OPEN_STATUSES as OPEN, countCases, type CaseQuery } from "./interactive";
import { PillTabs, TabPanel, useProgrammes, type PillTab } from "./kit";
import { plain } from "./shared";
import { useRouter } from "next/navigation";

const iconFor = (activity: string): DuoName => (/cancer|screen/.test(activity) ? "ribbon" : "heart");
const total = (params: Record<string, string>, statuses: string[], signal: AbortSignal) => countCases({ ...params, status: statuses }, signal);

interface Program {
  activity: string;
  name: string;
  blurb: string;
  total: number;
  open: number;
  completed: number;
}

type Tab = "programs" | "journey" | "operations" | "guidelines";
const TABS: PillTab<Tab>[] = [
  { key: "programs", label: "Programs", icon: "layers" },
  { key: "journey", label: "Care journey", icon: "activity" },
  { key: "operations", label: "Operations", icon: "building" },
  { key: "guidelines", label: "Guidelines", icon: "scale" },
];

function Chevron({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button type="button" onClick={onClick} aria-label={label} className="grid size-9 shrink-0 place-items-center rounded-full text-ink-muted hover:bg-brand-soft hover:text-brand-ink">
      <Icon name="chevron-right" className="size-5" />
    </button>
  );
}

export function PreventivePrograms() {
  const router = useRouter();
  const [active, setActive] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<{ title: string; query: CaseQuery } | null>(null);
  const guidelines = useApi("programs-guidelines", (s) => apiList<Guideline>("/guidelines", { page_size: 50 }, s));
  const list = guidelines.data?.data ?? [];
  
  const snap = useProgrammes();
  const counts = {
    data: snap.data
      ? (snap.data.data.programmes.map((r) => {
          const g = list.find((x) => (x.versions ?? []).some((v) => v.care_activity === r.care_activity));
          return { activity: r.care_activity, name: g?.name ? plain(g.name) : humanize(r.care_activity), blurb: g?.description ?? (g?.topic ? humanize(g.topic) : "Guideline defined preventive care."), total: r.cases_total, open: r.open_care_gaps, completed: r.completed } satisfies Program;
        }) as Program[])
      : null,
    error: snap.error,
    reload: snap.reload,
  };
  const programs = counts.data ?? [];
  const pick = programs.find((p) => p.activity === active) ?? [...programs].sort((a, b) => b.total - a.total)[0];

  const actions = useApi(`programs-actions:${pick?.activity ?? "all"}`, async (s) => {
    const base: Record<string, string> = pick ? { care_activity: pick.activity } : {};
    const [high, contacted, evidence] = await Promise.all([total({ ...base, priority: "high" }, OPEN, s), total(base, ["outreach_completed"], s), total(base, ["awaiting_completion_evidence"], s)]);
    return { high, contacted, evidence };
  });
  const a = actions.data;
  const open = (title: string, query: CaseQuery) => setDrawer({ title: `${title}${pick ? `, ${pick.name}` : ""}`, query: { ...(pick ? { care_activity: pick.activity } : {}), ...query } });

  return (
    <>
      <PageHeader
        title="Preventive care programs"
        description="Design, run and monitor population scale preventive care across conditions."
        actions={<PillTabs tabs={TABS} active="programs" onChange={(k) => k !== "programs" && router.push(k === "journey" ? "/journey" : k === "operations" ? "/operations" : "/guidelines")} label="Program views" panelId="programs-panel" />}
      />
      <TabPanel id="programs-panel" label="Programs">
        {guidelines.error ? (
          <ErrorPanel error={guidelines.error} onRetry={guidelines.reload} title="Could not load the programs" />
        ) : !guidelines.data || (!counts.data && !counts.error) ? (
          <div role="status" aria-label="Loading programs" className="space-y-4">
            <div className="skeleton h-64" />
            <div className="skeleton h-48" />
          </div>
        ) : counts.error ? (
          <ErrorPanel error={counts.error} onRetry={counts.reload} title="Could not count care gaps" />
        ) : programs.length === 0 ? (
          <Card className="glass-strong p-8">
            <p className="font-semibold">No program has any cases yet</p>
            <p className="mt-1 text-ink-muted">A program appears here when its guideline has raised a care gap case.</p>
          </Card>
        ) : (
          <div className="space-y-6">
            <div className="grid gap-5 xl:grid-cols-[1.25fr_1fr_1fr] *:min-w-0">
              <Card as="section" aria-labelledby="hero-h" className="glass-strong relative overflow-hidden">
                <div aria-hidden="true" className="pointer-events-none absolute -right-8 -bottom-8 grid size-64 place-items-center rounded-full bg-radial from-brand-soft via-brand-soft/40 to-transparent">
                  <GlassTile name={iconFor(pick.activity)} size="lg" />
                </div>
                <div className="relative p-6">
                  <p className="text-sm font-semibold tracking-wide text-brand-ink">Active program</p>
                  <h2 id="hero-h" className="mt-3 font-serif text-3xl font-bold">{pick.name}</h2>
                  <p className="mt-2 max-w-[38ch] text-ink-soft">{plain(pick.blurb)}</p>
                  <Link href={`/care-gaps?care_activity=${pick.activity}`} className="mt-6 inline-flex min-h-11 items-center gap-2 rounded-full bg-brand px-6 font-semibold text-white shadow-card hover:bg-brand-hover">
                    View program <Icon name="arrow-right" className="size-4" />
                  </Link>
                </div>
              </Card>

              <section aria-label="Program figures" className="grid gap-3">
                {[
                  { icon: "population" as DuoName, k: "Eligible population", v: pick.total, title: "All cases", q: {} as CaseQuery },
                  { icon: "layers" as DuoName, k: "Active care gaps", v: pick.open, title: "Active care gaps", q: { status: OPEN } as CaseQuery },
                  { icon: "complete" as DuoName, k: "Care completed", v: pick.completed, title: "Care completed", q: { status: ["completed"] } as CaseQuery },
                ].map((r) => (
                  <div key={r.k} className="card glass-strong flex items-center gap-4 px-5 py-4">
                    <GlassTile name={r.icon} size="md" />
                    <div className="min-w-0 flex-1">
                      <p className="text-ink-soft">{r.k}</p>
                      <p className="tnum font-serif text-3xl font-bold">{fmtInt(r.v)}</p>
                    </div>
                    <Chevron onClick={() => open(r.title, r.q)} label={`Open the cases: ${r.k.toLowerCase()}`} />
                  </div>
                ))}
              </section>

              <Card as="section" aria-labelledby="nba-h" className="glass-strong">
                <CardHeader id="nba-h" title="Next best actions" aside={a ? <Pill tone={a.high > 0 ? "danger" : "ok"}>{fmtInt(a.high)} high priority</Pill> : undefined} />
                <ul aria-label="Next best actions" className="divide-y divide-line px-5 pb-3">
                  {actions.error ? (
                    <li className="py-3"><ErrorPanel error={actions.error} onRetry={actions.reload} title="Could not load actions" /></li>
                  ) : (
                    [
                      { t: "Review high priority cases", d: a ? `${fmtInt(a.high)} open cases` : "Counting", query: { priority: "high", status: OPEN } as CaseQuery },
                      { t: "Book appointments", d: a ? `${fmtInt(a.contacted)} residents contacted, no appointment yet` : "Counting", query: { status: ["outreach_completed"] } as CaseQuery },
                      { t: "Review completion evidence", d: a ? `${fmtInt(a.evidence)} cases waiting for evidence` : "Counting", query: { status: ["awaiting_completion_evidence"] } as CaseQuery },
                    ].map((x) => (
                      <li key={x.t} className="flex items-center gap-3 py-3.5">
                        <div className="min-w-0 flex-1">
                          <p className="font-semibold">{x.t}</p>
                          <p className="text-[0.8125rem] text-ink-muted">{x.d}. {pick.name}</p>
                        </div>
                        <Chevron onClick={() => open(x.t, x.query)} label={`Open the cases: ${x.t}`} />
                      </li>
                    ))
                  )}
                </ul>
              </Card>
            </div>

            <section aria-labelledby="all-h">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h2 id="all-h" className="font-serif text-2xl font-bold">All preventive care programs</h2>
                <DataLabel kind="live" />
              </div>
              <ul aria-label="Programs" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
                {programs.map((p) => (
                  <li key={p.activity} data-program={p.activity}>
                    <button type="button" aria-pressed={p.activity === pick.activity} onClick={() => setActive(p.activity)} className={`glass-strong flex h-full w-full flex-col rounded-card border p-5 text-left transition-colors ${p.activity === pick.activity ? "border-brand ring-2 ring-brand/30" : "border-line hover:bg-canvas"}`}>
                      <GlassTile name={iconFor(p.activity)} size="md" />
                      <span className="mt-4 text-lg font-bold">{p.name}</span>
                      <span className="mt-1 line-clamp-2 flex-1 text-ink-muted">{plain(p.blurb)}</span>
                      <span className="tnum mt-3 text-[0.8125rem] text-ink-soft">{fmtInt(p.total)} cases, {fmtInt(p.completed)} completed</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        )}
      </TabPanel>
      <CaseDrawer open={drawer != null} onClose={() => setDrawer(null)} title={drawer?.title ?? ""} query={drawer?.query ?? {}} />
    </>
  );
}
