"use client";

// Interaction pieces shared by the journey, impact, programs, operations, planning and evidence pages: the illustrated stage
// glyph, a bar that grows when it appears, a hover and focus definition card, a right hand drawer (dialog) and the case
// drawer that shows the real care gap cases behind any number. Built on the design system tokens. Reduced motion is honoured
// by the global rule that shortens transitions. Purpose: make the screens explorable, not just readable.

import Link from "next/link";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { api, type Envelope } from "@/lib/api";
import { fmtDate, fmtInt, humanize } from "@/lib/format";
import type { CareGap } from "@/lib/types";
import { prefetchApi, useApi } from "@/lib/use-api";
import { StatusBadge } from "@/components/status-badge";
import { Pager, PriorityPill, GlassTile, type DuoName, Icon } from "@/components/ui";

// ---- stage glyph: a duotone icon on a glass tile with a soft halo and platform (no clip art) ----
export function StageArch({ icon }: { icon: DuoName }) {
  return (
    <div aria-hidden="true" className="relative mx-auto grid h-32 w-full max-w-44 place-items-center">
      <span className="absolute size-28 rounded-full bg-radial from-brand-soft via-brand-soft/50 to-transparent" />
      <span className="absolute bottom-2 h-5 w-28 rounded-[50%] bg-linear-to-b from-brand-soft to-white/0" />
      <span className="relative z-10">
        <GlassTile name={icon} size="lg" />
      </span>
    </div>
  );
}

/** Round chevron between stage cards. */
export const StageLink = () => (
  <span aria-hidden="true" className="hidden size-8 shrink-0 place-items-center rounded-full border border-line bg-surface text-brand-ink shadow-card lg:grid">
    <Icon name="chevron-right" className="size-4" />
  </span>
);

// ---- bar that grows from zero when it appears: scaleX from the left edge, so no layout property is animated ----
export function GrowBar({ pct, className = "bg-linear-to-r from-brand to-[#9b83e6]" }: { pct: number; className?: string }) {
  const [on, setOn] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setOn(true));
    return () => cancelAnimationFrame(id);
  }, []);
  return <span className={`block h-full rounded-full ${className}`} style={{ width: `${Math.max(0, Math.min(100, pct))}%`, transform: on ? "scaleX(1)" : "scaleX(0)", transformOrigin: "left center", transition: "transform 700ms cubic-bezier(0.2, 0.7, 0.2, 1)" }} />;
}

// ---- hover and focus definition card ----
export function InfoTip({ text, label = "What this means" }: { text: string; label?: string }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-flex align-middle" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" aria-label={label} aria-describedby={open ? id : undefined} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onKeyDown={(e) => e.key === "Escape" && setOpen(false)} className="grid size-6 place-items-center rounded-full text-ink-muted hover:text-brand-ink">
        <Icon name="info" className="size-4" />
      </button>
      {open && (
        <span role="tooltip" id={id} className="absolute top-full left-1/2 z-30 mt-1 w-64 -translate-x-1/2 rounded-card border border-line bg-surface p-3 text-left text-[0.8125rem] leading-snug font-normal text-ink-soft shadow-pop">
          {text}
        </span>
      )}
    </span>
  );
}

// ---- right hand drawer: Escape closes, focus moves in and returns, Tab stays inside ----
export function Drawer({ open, title, subtitle, onClose, children }: { open: boolean; title: string; subtitle?: ReactNode; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const raf = requestAnimationFrame(() => {
      setShown(true);
      ref.current?.focus();
    });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key !== "Tab" || !ref.current) return;
      const f = [...ref.current.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), select, input, [tabindex]:not([tabindex="-1"])')];
      if (f.length === 0) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKey);
      prev?.focus();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50">
      <button type="button" aria-label="Close the panel" tabIndex={-1} onClick={onClose} className={`absolute inset-0 bg-ink/30 transition-opacity duration-300 ${shown ? "opacity-100" : "opacity-0"}`} />
      <div ref={ref} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} className={`absolute inset-y-0 right-0 flex w-[min(38rem,100vw)] flex-col bg-surface shadow-pop transition-transform duration-300 ease-out ${shown ? "translate-x-0" : "translate-x-full"}`}>
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <h2 className="font-serif text-xl font-bold">{title}</h2>
            {subtitle && <p className="text-[0.8125rem] text-ink-muted">{subtitle}</p>}
          </div>
          <button type="button" aria-label="Close" onClick={onClose} className="grid size-9 shrink-0 place-items-center rounded-full border border-line hover:bg-canvas">
            <Icon name="close" className="size-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  );
}

// ---- care gap counts and the case drawer ----
export interface CaseQuery {
  care_activity?: string;
  district?: string;
  priority?: string;
  status?: string[];
}

/** Case statuses at or beyond each journey stage (backend/anticipa/caregaps/lifecycle.py ALLOWED). */
const OUTREACH = ["outreach_pending", "outreach_completed", "appointment_scheduled", "awaiting_completion_evidence", "completed"];
export const STAGE_STATUS: Record<string, string[]> = {
  eligible: [],
  planned: ["planned", ...OUTREACH],
  contacted: OUTREACH,
  attended: ["awaiting_completion_evidence", "completed"],
  completed: ["completed"],
};
export const OPEN_STATUSES = ["identified", "pending_review", "approved_for_planning", "awaiting_capacity", "planned", "outreach_pending", "outreach_completed", "appointment_scheduled", "awaiting_completion_evidence", "escalated"];

const queryString = (q: CaseQuery, extra: string[] = []) =>
  [...extra, ...Object.entries(q).flatMap(([k, v]) => (k === "status" ? ((v as string[] | undefined) ?? []).map((s) => `status=${s}`) : v ? [`${k}=${encodeURIComponent(v as string)}`] : []))].join("&");

/** meta.total of a page_size=1 call: a cheap count of care gap cases for a filter. */
export async function countCases(q: CaseQuery, signal: AbortSignal): Promise<number> {
  const r: Envelope<CareGap[]> = await api<CareGap[]>(`/care-gaps?${queryString(q, ["page_size=1"])}`, { signal });
  return r.meta.total ?? 0;
}

const prefetchJourney = (id: string) => prefetchApi(`journey:${id}`, (s) => api(`/residents/${id}/journey`, { signal: s }));

function DrawerCases({ query }: { query: CaseQuery }) {
  const [page, setPage] = useState(1);
  const res = useApi(`case-drawer:${queryString(query)}:${page}`, (s) => api<CareGap[]>(`/care-gaps?${queryString(query, [`page=${page}`, "page_size=10"])}`, { signal: s }));
  const rows = res.data?.data ?? [];
  const meta = res.data?.meta;
  const single = (query.status?.length ?? 0) <= 1;
  const href = `/care-gaps?${[query.care_activity && `care_activity=${query.care_activity}`, query.priority && `priority=${query.priority}`, single && query.status?.[0] && `status=${query.status[0]}`].filter(Boolean).join("&")}`;
  if (res.error)
    return (
      <p role="alert" className="rounded-control border border-danger-line bg-danger-bg px-4 py-3 text-danger">
        {res.error.message}
      </p>
    );
  if (!res.data)
    return (
      <div role="status" aria-label="Loading cases" className="space-y-2">
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="skeleton h-16" />
        ))}
      </div>
    );
  return (
    <div className="space-y-3">
      <p className="text-ink-soft">
        <span className="tnum font-semibold">{fmtInt(meta?.total ?? rows.length)}</span> cases match.
      </p>
      {rows.length === 0 ? (
        <p className="rounded-control border border-dashed border-line-strong px-4 py-6 text-ink-muted">No case matches these filters.</p>
      ) : (
        <ul aria-label="Matching cases" className="space-y-2">
          {rows.map((c) => (
            <li key={c.id}>
              <Link href={`/residents/${c.resident_id}/journey`} onMouseEnter={() => prefetchJourney(c.resident_id)} onFocus={() => prefetchJourney(c.resident_id)} className="flex flex-wrap items-center gap-2 rounded-card border border-line px-4 py-3 hover:bg-brand-soft">
                <span className="font-semibold">{humanize(c.care_activity)}</span>
                <PriorityPill priority={c.priority} />
                <StatusBadge status={c.status} />
                <span className="ml-auto text-xs text-ink-muted">Opened {fmtDate(c.opened_at)}</span>
                <span className="w-full font-mono text-xs text-ink-muted">
                  Resident {c.resident_id.slice(0, 8)}
                  {c.district_code ? `, district ${c.district_code}` : ""}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      <Pager page={meta?.page ?? page} totalPages={meta?.total_pages} total={meta?.total} loading={res.loading} onPage={setPage} />
      <p>
        <Link href={href} className="inline-flex items-center gap-1 text-brand-ink underline underline-offset-2">
          Open the care gap list <Icon name="arrow-right" className="size-4" />
        </Link>
      </p>
    </div>
  );
}

/** Drill down drawer: the real cases behind a number. Fetches only while open. */
export function CaseDrawer({ open, onClose, title, subtitle, query }: { open: boolean; onClose: () => void; title: string; subtitle?: ReactNode; query: CaseQuery }) {
  return (
    <Drawer open={open} onClose={onClose} title={title} subtitle={subtitle}>
      <DrawerCases key={queryString(query)} query={query} />
    </Drawer>
  );
}
