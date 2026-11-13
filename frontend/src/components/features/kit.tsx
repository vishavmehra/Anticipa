"use client";

// Small page kit shared by the reference pages (journey, evidence, planning, operations, programs, impact): the pill
// tab bar that sits at the right of a page title, the illustrated stage arch, and the data hooks every page reads.
// Built on the design system (Card, Icon, tokens). Purpose: show judges the finished product screens.

import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { api } from "@/lib/api";
import type { CapacityOverview, EngagementOverview } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { type IconName, Icon } from "@/components/ui";
import type { CommandCenter, Funnel, Programmes } from "./payloads";

// ---- data hooks (one place, one cache key each) ----
export const useFunnel = () => useApi("funnel", (s) => api<Funnel>("/analytics/funnel", { signal: s }));
export const useCommandCenter = () => useApi("command-center", (s) => api<CommandCenter>("/analytics/command-center", { signal: s }));
export const useProgrammes = () => useApi("programmes", (s) => api<Programmes>("/analytics/programmes", { signal: s }));
export const useCapacity = () => useApi("capacity", (s) => api<CapacityOverview>("/analytics/capacity", { signal: s }));
export const useEngagementOverview = () => useApi("engagement-overview", (s) => api<EngagementOverview>("/analytics/engagement", { signal: s }));

// ---- pill tabs ----
export interface PillTab<K extends string> {
  key: K;
  label: string;
  icon?: IconName;
}

/** Rounded tab bar (WAI tabs pattern: arrow keys move, one tab stop). The caller renders the matching panel with `tabPanelProps`. */
export function PillTabs<K extends string>({ tabs, active, onChange, label, panelId }: { tabs: readonly PillTab<K>[]; active: K; onChange: (k: K) => void; label: string; panelId: string }) {
  const base = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const move = (e: KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const n = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + d + tabs.length) % tabs.length;
    onChange(tabs[n].key);
    refs.current[n]?.focus();
  };
  return (
    <div role="tablist" aria-label={label} className="flex max-w-full flex-wrap gap-1 rounded-full border border-line bg-surface p-1 shadow-card">
      {tabs.map((t, i) => {
        const on = t.key === active;
        return (
          <button
            key={t.key}
            ref={(el) => {
              refs.current[i] = el;
            }}
            id={`${base}-${t.key}`}
            role="tab"
            type="button"
            aria-selected={on}
            aria-controls={panelId}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(t.key)}
            onKeyDown={(e) => move(e, i)}
            className={`inline-flex min-h-9 items-center gap-2 rounded-full px-4 text-sm font-semibold transition-colors ${on ? "bg-brand text-white shadow-sm" : "text-ink-soft hover:bg-brand-soft hover:text-brand-ink"}`}
          >
            {t.icon && <Icon name={t.icon} className="size-4" />}
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/** The panel a PillTabs bar controls: pass the same id to both. */
export function TabPanel({ children, label, id }: { children: ReactNode; label: string; id: string }) {
  return (
    <div id={id} role="tabpanel" aria-label={label} tabIndex={-1}>
      {children}
    </div>
  );
}

// The stage glyph, grow bar, tooltip and drawers live in interactive.tsx; re-exported so pages import one kit.
export { CaseDrawer, Drawer, GrowBar, InfoTip, OPEN_STATUSES, STAGE_STATUS, StageArch, StageLink, countCases, type CaseQuery } from "./interactive";
export { DuoIcon, GlassTile, type DuoName } from "@/components/ui";
