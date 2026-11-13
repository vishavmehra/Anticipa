// One duotone icon family for the whole app: this file holds the hand drawn glyphs, ui/icons.tsx holds the named UI icons
// drawn in the same style (stroke 1.8, round caps and joins, soft tint). Import everything from "@/components/ui".
// (moved from features/duotone.tsx) One coherent duotone icon set for the journey, impact, planning, evidence, operations and programs pages. Hand drawn on a
// 32 by 32 grid with a single 1.8 stroke, round caps and joins, and a soft tint fill: the stroke and the tint both come from
// currentColor (the brand ink), so the set follows the design tokens. Decorative by default (aria-hidden); the label next to
// an icon carries the meaning. Callers: kit StageGlyph, journey, impact, programs, planning studio, evidence extraction.

import type { ReactNode } from "react";

export type DuoName =
  | "read" | "decide" | "plan" | "engage" | "attend" | "complete" | "population" | "eligible" | "calendar" | "pin" | "shield"
  | "assistant" | "ribbon" | "chart" | "clock" | "filter" | "heart" | "building" | "note" | "target" | "layers";

const S = { stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" } as const;
const T = { fill: "currentColor", fillOpacity: 0.16, stroke: "none" } as const; // the tint layer

const GLYPH: Record<DuoName, ReactNode> = {
  assistant: (
    <>
      <path d="M16 3.8v4.4" {...S} />
      <circle cx="16" cy="3.6" r="1.7" fill="currentColor" />
      <rect x="5" y="8.2" width="22" height="17" rx="6.2" {...T} />
      <rect x="5" y="8.2" width="22" height="17" rx="6.2" {...S} />
      <circle cx="11.8" cy="16" r="1.9" fill="currentColor" />
      <circle cx="20.2" cy="16" r="1.9" fill="currentColor" />
      <path d="M12.4 20.6c1 .9 2.2 1.3 3.6 1.3s2.6-.4 3.6-1.3" {...S} />
      <path d="M2.8 14v4.4M29.2 14v4.4" {...S} />
    </>
  ),
  population: (
    <>
      <circle cx="21" cy="11" r="4" {...T} />
      <path d="M15 26v-1.2a5 5 0 0 1 5-5h2a5 5 0 0 1 5 5V26z" {...T} />
      <circle cx="21" cy="11" r="4" {...S} />
      <path d="M16.5 20.6A5 5 0 0 1 20 19.8h2a5 5 0 0 1 5 5V26" {...S} />
      <circle cx="12" cy="12" r="4.6" {...T} />
      <path d="M3.5 27v-1.5a6.2 6.2 0 0 1 6.2-6.2h4.6a6.2 6.2 0 0 1 6.2 6.2V27z" {...T} />
      <circle cx="12" cy="12" r="4.6" {...S} />
      <path d="M3.5 27v-1.5a6.2 6.2 0 0 1 6.2-6.2h4.6a6.2 6.2 0 0 1 6.2 6.2V27" {...S} />
    </>
  ),
  eligible: (
    <>
      <circle cx="14" cy="11" r="5" {...T} />
      <circle cx="14" cy="11" r="5" {...S} />
      <path d="M5 26v-1.5a6 6 0 0 1 6-6h6a6 6 0 0 1 3.2 .9" {...S} />
      <circle cx="23.5" cy="23" r="5" {...T} />
      <path d="M21 23.2l1.8 1.8 3.4-3.6" {...S} />
    </>
  ),
  read: (
    <>
      <rect x="6" y="4" width="16" height="22" rx="3.5" {...T} />
      <rect x="6" y="4" width="16" height="22" rx="3.5" {...S} />
      <path d="M10 10h8M10 14h8M10 18h4" {...S} />
      <circle cx="22.5" cy="22.5" r="4.2" fill="#fff" fillOpacity="0.9" stroke="none" />
      <circle cx="22.5" cy="22.5" r="4.2" {...S} />
      <path d="M25.6 25.6L28 28" {...S} />
    </>
  ),
  decide: (
    <>
      <path d="M16 5v21M10 26h12" {...S} />
      <path d="M6 9.5h20" {...S} />
      <path d="M6 9.5L2.8 17a3.6 3.6 0 0 0 6.4 0z" {...T} />
      <path d="M6 9.5L2.8 17a3.6 3.6 0 0 0 6.4 0z" {...S} />
      <path d="M26 9.5L22.8 17a3.6 3.6 0 0 0 6.4 0z" {...T} />
      <path d="M26 9.5L22.8 17a3.6 3.6 0 0 0 6.4 0z" {...S} />
    </>
  ),
  plan: (
    <>
      <rect x="6" y="6" width="20" height="21" rx="3.5" {...T} />
      <rect x="6" y="6" width="20" height="21" rx="3.5" {...S} />
      <rect x="11.5" y="3.5" width="9" height="5" rx="2" fill="#fff" fillOpacity="0.9" stroke="none" />
      <rect x="11.5" y="3.5" width="9" height="5" rx="2" {...S} />
      <path d="M10.5 15l1.7 1.7 3-3.2M18 15.5h4.5M10.5 21.5l1.7 1.7 3-3.2M18 22h4.5" {...S} />
    </>
  ),
  engage: (
    <>
      <path d="M5 8.5A3.5 3.5 0 0 1 8.5 5h15A3.5 3.5 0 0 1 27 8.5v9A3.5 3.5 0 0 1 23.5 21H15l-5.5 5v-5H8.5A3.5 3.5 0 0 1 5 17.5z" {...T} />
      <path d="M5 8.5A3.5 3.5 0 0 1 8.5 5h15A3.5 3.5 0 0 1 27 8.5v9A3.5 3.5 0 0 1 23.5 21H15l-5.5 5v-5H8.5A3.5 3.5 0 0 1 5 17.5z" {...S} />
      <circle cx="11" cy="13" r="1.3" fill="currentColor" />
      <circle cx="16" cy="13" r="1.3" fill="currentColor" />
      <circle cx="21" cy="13" r="1.3" fill="currentColor" />
    </>
  ),
  attend: (
    <>
      <path d="M6 27V10.5L16 5l10 5.5V27z" {...T} />
      <path d="M6 27V10.5L16 5l10 5.5V27M3.5 27h25" {...S} />
      <path d="M16 11v6M13 14h6" {...S} />
      <path d="M13 27v-5a3 3 0 0 1 6 0v5" {...S} />
    </>
  ),
  complete: (
    <>
      <circle cx="16" cy="16" r="11.5" {...T} />
      <circle cx="16" cy="16" r="11.5" {...S} />
      <path d="M10.5 16.5l4 4 7.5-8" {...S} />
    </>
  ),
  calendar: (
    <>
      <rect x="4.5" y="6" width="23" height="21" rx="4" {...T} />
      <rect x="4.5" y="6" width="23" height="21" rx="4" {...S} />
      <path d="M4.5 12.5h23M10.5 3.5v5M21.5 3.5v5" {...S} />
      <path d="M10.5 18.5h2M15 18.5h2M19.5 18.5h2M10.5 22.5h2M15 22.5h2" {...S} />
    </>
  ),
  pin: (
    <>
      <path d="M16 28s9-7.4 9-14.5a9 9 0 0 0-18 0C7 20.6 16 28 16 28z" {...T} />
      <path d="M16 28s9-7.4 9-14.5a9 9 0 0 0-18 0C7 20.6 16 28 16 28z" {...S} />
      <circle cx="16" cy="13.5" r="3.2" {...S} />
    </>
  ),
  shield: (
    <>
      <path d="M16 4l10 3.6v7.6c0 6-4.2 10.4-10 12.8C10.2 25.6 6 21.2 6 15.2V7.6z" {...T} />
      <path d="M16 4l10 3.6v7.6c0 6-4.2 10.4-10 12.8C10.2 25.6 6 21.2 6 15.2V7.6z" {...S} />
      <path d="M11.5 15.5l3.2 3.2 6-6.4" {...S} />
    </>
  ),
  ribbon: (
    <>
      <path d="M16 4a6.5 6.5 0 0 0-4.6 11.1L8 26.5l4-1.6 2.4 3.1L16 20l1.6 8 2.4-3.1 4 1.6-3.4-11.4A6.5 6.5 0 0 0 16 4z" {...T} />
      <path d="M16 4a6.5 6.5 0 0 0-4.6 11.1L8 26.5l4-1.6 2.4 3.1L16 20l1.6 8 2.4-3.1 4 1.6-3.4-11.4A6.5 6.5 0 0 0 16 4z" {...S} />
    </>
  ),
  chart: (
    <>
      <rect x="5" y="16" width="5" height="10" rx="1.5" {...T} />
      <rect x="13.5" y="9" width="5" height="17" rx="1.5" {...T} />
      <rect x="22" y="13" width="5" height="13" rx="1.5" {...T} />
      <rect x="5" y="16" width="5" height="10" rx="1.5" {...S} />
      <rect x="13.5" y="9" width="5" height="17" rx="1.5" {...S} />
      <rect x="22" y="13" width="5" height="13" rx="1.5" {...S} />
      <path d="M3.5 28.5h25" {...S} />
    </>
  ),
  clock: (
    <>
      <circle cx="16" cy="16" r="11.5" {...T} />
      <circle cx="16" cy="16" r="11.5" {...S} />
      <path d="M16 9.5V16l4.4 2.6" {...S} />
    </>
  ),
  filter: (
    <>
      <path d="M4.5 6.5h23l-8.7 10.2v7.6l-5.6 3.2v-10.8z" {...T} />
      <path d="M4.5 6.5h23l-8.7 10.2v7.6l-5.6 3.2v-10.8z" {...S} />
    </>
  ),
  heart: (
    <>
      <path d="M16 27S4.5 20 4.5 11.8A6.3 6.3 0 0 1 16 8a6.3 6.3 0 0 1 11.5 3.8C27.5 20 16 27 16 27z" {...T} />
      <path d="M16 27S4.5 20 4.5 11.8A6.3 6.3 0 0 1 16 8a6.3 6.3 0 0 1 11.5 3.8C27.5 20 16 27 16 27z" {...S} />
    </>
  ),
  building: (
    <>
      <rect x="5" y="5" width="15" height="22" rx="2.5" {...T} />
      <rect x="5" y="5" width="15" height="22" rx="2.5" {...S} />
      <path d="M20 13h5.5A1.5 1.5 0 0 1 27 14.5V27H20" {...S} />
      <path d="M9.5 10h6M9.5 14.5h6M9.5 19h6M12.5 27v-4" {...S} />
    </>
  ),
  note: (
    <>
      <path d="M8 4h11l6 6v16a2.5 2.5 0 0 1-2.5 2.5h-14.5A2.5 2.5 0 0 1 5.5 26V6.5A2.5 2.5 0 0 1 8 4z" {...T} />
      <path d="M8 4h11l6 6v16a2.5 2.5 0 0 1-2.5 2.5H8a2.5 2.5 0 0 1-2.5-2.5V6.5A2.5 2.5 0 0 1 8 4zM19 4v6h6" {...S} />
      <path d="M10 15h11M10 19.5h11M10 24h6" {...S} />
    </>
  ),
  target: (
    <>
      <circle cx="16" cy="16" r="11.5" {...T} />
      <circle cx="16" cy="16" r="11.5" {...S} />
      <circle cx="16" cy="16" r="6.2" {...S} />
      <circle cx="16" cy="16" r="1.6" fill="currentColor" />
    </>
  ),
  layers: (
    <>
      <path d="M16 4.5l12 6.3-12 6.3-12-6.3z" {...T} />
      <path d="M16 4.5l12 6.3-12 6.3-12-6.3z" {...S} />
      <path d="M4 16.2l12 6.3 12-6.3M4 21.8l12 6.3 12-6.3" {...S} />
    </>
  ),
};

/** Duotone glyph. Colour comes from the text colour of the parent (use text-brand-ink or a tone token). */
export function DuoIcon({ name, className = "size-7" }: { name: DuoName; className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true" focusable="false">
      {GLYPH[name]}
    </svg>
  );
}

/** Soft glass tile that holds a DuoIcon: a hairline white border, a pale tint ramp and a whisper of shadow. No blur. */
export function GlassTile({ name, size = "md", tone = "brand" }: { name: DuoName; size?: "sm" | "md" | "lg"; tone?: "brand" | "ok" | "warn" | "danger" | "info" }) {
  const box = { sm: "size-10 rounded-xl", md: "size-14 rounded-2xl", lg: "size-20 rounded-3xl" }[size];
  const icon = { sm: "size-6", md: "size-8", lg: "size-12" }[size];
  const color = { brand: "text-brand-ink from-white to-brand-soft", ok: "text-ok from-white to-ok-bg", warn: "text-warn from-white to-warn-bg", danger: "text-danger from-white to-danger-bg", info: "text-info from-white to-info-bg" }[tone];
  return (
    <span aria-hidden="true" className={`grid shrink-0 place-items-center border border-white bg-linear-to-b shadow-card ring-1 ring-black/5 ${box} ${color}`}>
      <DuoIcon name={name} className={icon} />
    </span>
  );
}
