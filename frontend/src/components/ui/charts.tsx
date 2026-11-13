"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";

// Hand-built, dependency-free charts (dataviz method: thin marks, 2px surface gaps, recessive grid, legend for >= 2
// series, text tokens for labels, a table alternative behind every chart). Marks validated >= 3:1 on white.
export const CHART = {
  // "teal" and "tealDark" keep their old key names for existing callers; every value is a theme token.
  teal: "var(--chart-teal)",
  tealDark: "var(--chart-teal-dark)",
  blue: "var(--chart-blue)",
  red: "var(--chart-red)",
  amber: "var(--chart-amber)",
  green: "var(--chart-green)",
  navy: "var(--chart-navy)",
  violet: "var(--chart-violet)",
  mid: "var(--chart-mid)",
  light: "var(--chart-light)",
  grid: "var(--chart-grid)",
  axis: "var(--chart-axis)",
  track: "var(--chart-track)",
  empty: "var(--chart-empty)",
} as const;

/** Sequential violet scale, dark (high) to light (low). Marks are >= 3:1 on white except the last, which is a fill under text. */
export const CHART_SCALE = ["var(--scale-1)", "var(--scale-2)", "var(--scale-3)", "var(--scale-4)", "var(--scale-5)"] as const;

/** Smooth line through points (Catmull-Rom converted to cubic Bezier), so a trend reads as a curve, not a polyline. */
function smooth(pts: { x: number; y: number }[]): string {
  if (pts.length < 3) return pts.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  let d = `M${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] ?? pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] ?? p2;
    const c1 = { x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6 };
    const c2 = { x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6 };
    d += ` C${c1.x.toFixed(1)},${c1.y.toFixed(1)} ${c2.x.toFixed(1)},${c2.y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
  }
  return d;
}

/** Container width in px (ResizeObserver), so SVG text stays 12px at any viewport instead of scaling down. */
function useWidth(initial = 560) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(220, Math.floor(e.contentRect.width))));
    ro.observe(el);
    setW(Math.max(220, Math.floor(el.getBoundingClientRect().width)));
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

/** Table alternative for a chart: always available, one click away. */
export function ChartTable({ caption, columns, rows }: { caption: string; columns: string[]; rows: ReactNode[][] }) {
  return (
    <details className="mt-3 text-[0.8125rem]">
      <summary className="inline-block cursor-pointer rounded-sm font-medium text-brand-ink underline underline-offset-2">View as table</summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full border-collapse text-left">
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr className="border-b border-line text-ink-muted">
              {columns.map((c, i) => (
                <th key={c} scope="col" className={`px-2 py-1 font-medium ${i ? "text-right" : ""}`}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-b border-line last:border-0">
                {r.map((c, j) => (
                  <td key={j} className={`tnum px-2 py-1 ${j ? "text-right" : ""}`}>
                    {c}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

// ---- Donut ----------------------------------------------------------------------------------------------------------
export interface Slice {
  label: string;
  value: number;
  color: string;
}

/** Share-of-whole donut with a legend that carries the numbers (identity is never colour alone). */
export function Donut({ slices, centerValue, centerLabel, ariaLabel, tableCaption, valueFmt = (n) => n.toLocaleString("en-US") }: { slices: Slice[]; centerValue: ReactNode; centerLabel: string; ariaLabel: string; tableCaption: string; valueFmt?: (n: number) => string }) {
  const total = slices.reduce((s, x) => s + x.value, 0);
  const R = 62;
  const C = 2 * Math.PI * R;
  const gap = total > 0 && slices.filter((s) => s.value > 0).length > 1 ? 3 : 0;
  let offset = 0;
  return (
    <div>
      <div className="flex flex-wrap items-center justify-center gap-x-8 gap-y-4">
        <div className="relative size-44 shrink-0">
          <svg viewBox="0 0 160 160" role="img" aria-label={ariaLabel} className="size-full -rotate-90">
            <circle cx="80" cy="80" r={R} fill="none" stroke={CHART.grid} strokeWidth="22" />
            {total > 0 &&
              slices.map((s) => {
                const len = (s.value / total) * C;
                const dash = Math.max(0, len - gap);
                const el = <circle key={s.label} cx="80" cy="80" r={R} fill="none" stroke={s.color} strokeWidth="22" strokeDasharray={`${dash} ${C - dash}`} strokeDashoffset={-offset} />;
                offset += len;
                return el;
              })}
          </svg>
          <div className="pointer-events-none absolute inset-0 grid place-items-center text-center">
            <div>
              <p className="tnum text-xl leading-6 font-bold">{centerValue}</p>
              <p className="text-xs text-ink-muted">{centerLabel}</p>
            </div>
          </div>
        </div>
        <ul className="min-w-44 space-y-2">
          {slices.map((s) => (
            <li key={s.label} className="flex items-center gap-2.5">
              <span aria-hidden="true" className="size-3 rounded-full" style={{ background: s.color }} />
              <span className="w-16 font-medium">{s.label}</span>
              <span className="tnum text-ink">{valueFmt(s.value)}</span>
              <span className="tnum text-ink-muted">({total ? ((s.value / total) * 100).toFixed(1) : "0.0"}%)</span>
            </li>
          ))}
        </ul>
      </div>
      <ChartTable caption={tableCaption} columns={["Group", "Count", "Share"]} rows={slices.map((s) => [s.label, valueFmt(s.value), `${total ? ((s.value / total) * 100).toFixed(1) : "0.0"}%`])} />
    </div>
  );
}

// ---- AreaTrend -------------------------------------------------------------------------------------------------------
export interface TrendPoint {
  label: string;
  value: number;
}

/** Single-series area/line over time with crosshair tooltip (hover and keyboard focus). y axis starts at 0. */
export function AreaTrend({ points, height = 220, yMax, yFormat = (n) => Math.round(n).toLocaleString("en-US"), ariaLabel, tableCaption, seriesLabel = "Value" }: { points: TrendPoint[]; height?: number; yMax?: number; yFormat?: (n: number) => string; ariaLabel: string; tableCaption: string; seriesLabel?: string }) {
  const [ref, w] = useWidth();
  const [active, setActive] = useState<number | null>(null);
  const padL = 56;
  const padR = 16;
  const padT = 14;
  const padB = 28;
  const max = yMax ?? Math.max(1, ...points.map((p) => p.value)) * 1.1;
  const ticks = 4;
  const x = (i: number) => padL + (points.length > 1 ? (i / (points.length - 1)) * (w - padL - padR) : (w - padL - padR) / 2);
  const y = (v: number) => padT + (1 - v / max) * (height - padT - padB);
  const line = smooth(points.map((p, i) => ({ x: x(i), y: Math.max(padT, Math.min(height - padB, y(p.value))) })));
  const area = points.length ? `${line} L${x(points.length - 1).toFixed(1)},${y(0)} L${x(0).toFixed(1)},${y(0)} Z` : "";
  const labelEvery = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor((w - padL) / 64))));
  const a = active !== null ? points[active] : null;
  const gid = useId();
  return (
    <div>
      <div ref={ref} className="relative">
        <svg width={w} height={height} role="img" aria-label={ariaLabel} onMouseLeave={() => setActive(null)}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor={CHART.teal} stopOpacity="0.28" />
              <stop offset="1" stopColor={CHART.teal} stopOpacity="0.02" />
            </linearGradient>
          </defs>
          {Array.from({ length: ticks + 1 }, (_, i) => {
            const v = (max / ticks) * i;
            return (
              <g key={i}>
                <line x1={padL} x2={w - padR} y1={y(v)} y2={y(v)} stroke={CHART.grid} strokeWidth="1" />
                <text x={padL - 8} y={y(v) + 4} textAnchor="end" fontSize="12" fill={CHART.axis}>
                  {yFormat(v)}
                </text>
              </g>
            );
          })}
          {points.map((p, i) =>
            i % labelEvery === 0 ? (
              <text key={`${i}-${p.label}`} x={x(i)} y={height - 8} textAnchor={i === points.length - 1 ? "end" : "middle"} fontSize="12" fill={CHART.axis}>
                {p.label}
              </text>
            ) : null,
          )}
          {points.length > 0 && <path d={area} fill={`url(#${gid})`} />}
          {points.length > 0 && <path d={line} fill="none" stroke={CHART.tealDark} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />}
          {a && active !== null && <line x1={x(active)} x2={x(active)} y1={padT} y2={height - padB} stroke={CHART.axis} strokeDasharray="3 3" />}
          {points.map((p, i) => (
            <g key={`${i}-${p.label}`}>
              {(points.length <= 14 || active === i || i === points.length - 1) && <circle cx={x(i)} cy={y(p.value)} r={active === i ? 5 : 4} fill="#fff" stroke={CHART.tealDark} strokeWidth="2" />}
              <rect
                x={x(i) - Math.max(10, (w - padL - padR) / Math.max(1, points.length) / 2)}
                y={padT}
                width={Math.max(20, (w - padL - padR) / Math.max(1, points.length))}
                height={height - padT - padB}
                fill="transparent"
                tabIndex={0}
                role="img"
                aria-label={`${p.label}: ${yFormat(p.value)}`}
                onMouseEnter={() => setActive(i)}
                onFocus={() => setActive(i)}
                onBlur={() => setActive(null)}
                className="outline-offset-[-2px]"
              />
            </g>
          ))}
        </svg>
        {a && active !== null && (
          <div
            className="pointer-events-none absolute -translate-x-1/2 -translate-y-full rounded-md bg-navy px-2.5 py-1.5 text-xs font-medium whitespace-nowrap text-white shadow-pop"
            style={{ left: Math.min(Math.max(x(active), 60), w - 60), top: y(a.value) - 10 }}
          >
            {a.label}: <span className="tnum font-bold">{yFormat(a.value)}</span>
          </div>
        )}
      </div>
      <ChartTable caption={tableCaption} columns={["Period", seriesLabel]} rows={points.map((p) => [p.label, yFormat(p.value)])} />
    </div>
  );
}

// ---- BarList ---------------------------------------------------------------------------------------------------------
export interface BarRow {
  label: string;
  value: number;
  display?: string;
  note?: string;
  color?: string;
}

/** Horizontal bars with the value written at the end of each row (direct labels; no legend needed for one series). */
export function BarList({ rows, max, ariaLabel, tableCaption, valueHeader = "Value", labelWidth = "w-36" }: { rows: BarRow[]; max?: number; ariaLabel: string; tableCaption: string; valueHeader?: string; labelWidth?: string }) {
  const top = max ?? Math.max(1, ...rows.map((r) => r.value));
  return (
    <div>
      <ul aria-label={ariaLabel} className="space-y-2.5">
        {rows.map((r) => (
          <li key={r.label} className="flex items-center gap-3">
            <span className={`${labelWidth} shrink-0 text-ink-soft`}>{r.label}</span>
            <span className="h-3 min-w-10 flex-1 overflow-hidden rounded-full bg-neutral-bg" aria-hidden="true">
              <span className="block h-full rounded-full" style={{ width: `${Math.max(0, Math.min(100, (r.value / top) * 100))}%`, background: r.color ?? `linear-gradient(90deg, ${CHART.violet}, ${CHART.mid})` }} />
            </span>
            <span className="tnum w-36 shrink-0 text-right font-semibold whitespace-nowrap">
              {r.display ?? r.value.toLocaleString("en-US")}
              {r.note && <span className="ml-1 font-normal text-ink-muted">{r.note}</span>}
            </span>
          </li>
        ))}
      </ul>
      <ChartTable caption={tableCaption} columns={["Group", valueHeader]} rows={rows.map((r) => [r.label, `${r.display ?? r.value.toLocaleString("en-US")}${r.note ? ` ${r.note}` : ""}`])} />
    </div>
  );
}

// ---- ColumnPairs -----------------------------------------------------------------------------------------------------
export interface PairGroup {
  label: string;
  a: number;
  b: number;
}

/** Grouped columns for two measures on ONE axis (e.g. demand vs capacity); never dual-axis. */
export function ColumnPairs({ groups, aLabel, bLabel, height = 220, ariaLabel, tableCaption, yFormat = (n) => n.toLocaleString("en-US") }: { groups: PairGroup[]; aLabel: string; bLabel: string; height?: number; ariaLabel: string; tableCaption: string; yFormat?: (n: number) => string }) {
  const [ref, w] = useWidth();
  const padL = 48;
  const padB = 28;
  const padT = 12;
  const max = Math.max(1, ...groups.flatMap((g) => [g.a, g.b])) * 1.1;
  const ticks = 4;
  const gw = (w - padL - 8) / Math.max(1, groups.length);
  const bw = Math.max(6, Math.min(26, gw / 3));
  const y = (v: number) => padT + (1 - v / max) * (height - padT - padB);
  return (
    <div>
      <div ref={ref}>
        <svg width={w} height={height} role="img" aria-label={ariaLabel}>
          {Array.from({ length: ticks + 1 }, (_, i) => {
            const v = (max / ticks) * i;
            return (
              <g key={i}>
                <line x1={padL} x2={w - 8} y1={y(v)} y2={y(v)} stroke={CHART.grid} />
                <text x={padL - 8} y={y(v) + 4} textAnchor="end" fontSize="12" fill={CHART.axis}>
                  {yFormat(v)}
                </text>
              </g>
            );
          })}
          {groups.map((g, i) => {
            const cx = padL + gw * i + gw / 2;
            return (
              <g key={g.label}>
                <title>{`${g.label}: ${aLabel} ${yFormat(g.a)}, ${bLabel} ${yFormat(g.b)}`}</title>
                <rect x={cx - bw - 1} y={y(g.a)} width={bw} height={Math.max(0, y(0) - y(g.a))} rx="4" fill={CHART.teal} />
                <rect x={cx + 1} y={y(g.b)} width={bw} height={Math.max(0, y(0) - y(g.b))} rx="4" fill={CHART.mid} />
                <text x={cx} y={height - 8} textAnchor="middle" fontSize="12" fill={CHART.axis}>
                  {g.label}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <ul className="mt-1 flex flex-wrap gap-x-5 gap-y-1 text-[0.8125rem] text-ink-soft">
        <li className="flex items-center gap-1.5">
          <span aria-hidden="true" className="size-3 rounded-sm" style={{ background: CHART.teal }} />
          {aLabel}
        </li>
        <li className="flex items-center gap-1.5">
          <span aria-hidden="true" className="size-3 rounded-sm" style={{ background: CHART.mid }} />
          {bLabel}
        </li>
      </ul>
      <ChartTable caption={tableCaption} columns={["Group", aLabel, bLabel]} rows={groups.map((g) => [g.label, yFormat(g.a), yFormat(g.b)])} />
    </div>
  );
}

// ---- Sparkline -------------------------------------------------------------------------------------------------------
/** Tiny trend line next to a stated number. Decorative: the figure beside it carries the meaning. */
export function Sparkline({ values, width = 92, height = 34, color = CHART.teal, fluid = false }: { values: number[]; width?: number; height?: number; color?: string; fluid?: boolean }) {
  const gid = useId();
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 3;
  const x = (i: number) => pad + (i / (values.length - 1)) * (width - pad * 2);
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - pad * 2);
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg width={fluid ? "100%" : width} height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio={fluid ? "none" : undefined} aria-hidden="true" className="shrink-0">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.25" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${line} L${x(values.length - 1)},${height} L${x(0)},${height} Z`} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

// ---- Gauge -----------------------------------------------------------------------------------------------------------
/** Half-circle gauge for one bounded score. `value === null` shows "Not available", never an empty arc that reads as zero. */
export function Gauge({ value, max = 100, label, caption, ariaLabel, format = (n) => String(Math.round(n)) }: { value: number | null; max?: number; label: string; caption?: ReactNode; ariaLabel: string; format?: (n: number) => string }) {
  const gid = useId();
  const L = Math.PI * 64;
  const frac = value === null ? 0 : Math.max(0, Math.min(1, value / max));
  return (
    <div className="text-center">
      <div className="relative mx-auto w-full max-w-52">
        <svg viewBox="0 0 160 96" role="img" aria-label={value === null ? `${ariaLabel}: not available` : `${ariaLabel}: ${format(value)} of ${max}`} className="w-full">
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stopColor={CHART.mid} />
              <stop offset="1" stopColor={CHART.tealDark} />
            </linearGradient>
          </defs>
          <path d="M16 86 A64 64 0 0 1 144 86" fill="none" stroke="var(--chart-track)" strokeWidth="14" strokeLinecap="round" />
          {value !== null && <path d="M16 86 A64 64 0 0 1 144 86" fill="none" stroke={`url(#${gid})`} strokeWidth="14" strokeLinecap="round" strokeDasharray={`${frac * L} ${L}`} />}
        </svg>
        <div className="absolute inset-x-0 bottom-0 text-center">
          <p className="tnum font-display text-4xl leading-none font-bold text-ink">{value === null ? "None" : format(value)}</p>
          <p className="mt-0.5 text-xs text-ink-muted">{value === null ? "Not available" : `of ${max}`}</p>
        </div>
      </div>
      <p className="mt-1 text-[0.8125rem] font-semibold text-ink-soft">{label}</p>
      {caption && <p className="text-xs text-ink-muted">{caption}</p>}
    </div>
  );
}

// ---- FunnelBars ------------------------------------------------------------------------------------------------------
export interface FunnelRow {
  label: string;
  value: number;
  /** text shown at the end (defaults to the share of the first row) */
  display?: string;
}

/** Stage-to-stage funnel: each bar is scaled to the FIRST stage, shades step from dark to light. */
export function FunnelBars({ rows, ariaLabel, tableCaption }: { rows: FunnelRow[]; ariaLabel: string; tableCaption: string }) {
  const top = rows[0]?.value || 0;
  const share = (v: number) => (top > 0 ? `${((v / top) * 100).toFixed(v / top < 0.1 ? 1 : 0)}%` : "None");
  return (
    <div>
      <ul aria-label={ariaLabel} className="space-y-3">
        {rows.map((r, i) => (
          <li key={r.label} className="flex items-center gap-3">
            <span className="w-24 shrink-0 text-ink-soft">{r.label}</span>
            <span className="h-3 min-w-10 flex-1 overflow-hidden rounded-full bg-neutral-bg" aria-hidden="true">
              <span className="block h-full rounded-full" style={{ width: `${top > 0 ? Math.max(1.5, Math.min(100, (r.value / top) * 100)) : 0}%`, background: CHART_SCALE[Math.min(i, CHART_SCALE.length - 2)] }} />
            </span>
            <span className="tnum w-16 shrink-0 text-right font-semibold">{r.display ?? share(r.value)}</span>
          </li>
        ))}
      </ul>
      <ChartTable caption={tableCaption} columns={["Stage", "Count", "Share of first stage"]} rows={rows.map((r) => [r.label, r.value.toLocaleString("en-US"), share(r.value)])} />
    </div>
  );
}

// ---- StackedBar ------------------------------------------------------------------------------------------------------
export interface Segment {
  label: string;
  value: number;
  color: string;
}

/** One 100% bar for a split of a whole (for example sex). Legend carries the numbers. */
export function StackedBar({ segments, ariaLabel, tableCaption, valueFmt = (n) => n.toLocaleString("en-US") }: { segments: Segment[]; ariaLabel: string; tableCaption: string; valueFmt?: (n: number) => string }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  return (
    <div>
      <div role="img" aria-label={ariaLabel} className="flex h-4 gap-0.5 overflow-hidden rounded-full bg-neutral-bg">
        {total > 0 && segments.map((s) => <span key={s.label} style={{ width: `${(s.value / total) * 100}%`, background: s.color }} />)}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[0.8125rem] text-ink-soft">
        {segments.map((s) => (
          <li key={s.label} className="flex items-center gap-1.5">
            <span aria-hidden="true" className="size-3 rounded-full" style={{ background: s.color }} />
            {s.label}
            <span className="tnum font-semibold text-ink">{total ? `${((s.value / total) * 100).toFixed(1)}%` : "None"}</span>
          </li>
        ))}
      </ul>
      <ChartTable caption={tableCaption} columns={["Group", "Count", "Share"]} rows={segments.map((s) => [s.label, valueFmt(s.value), `${total ? ((s.value / total) * 100).toFixed(1) : "0.0"}%`])} />
    </div>
  );
}

// ---- Cartogram -------------------------------------------------------------------------------------------------------
export interface CartogramTile {
  code: string;
  /** human label (defaults to the code) */
  label?: string;
  /** numeric value used for shading; null renders a neutral tile */
  value: number | null;
  /** formatted value shown inside the tile */
  display: string;
}

/**
 * District cartogram: the dataset has district codes only (no boundaries), so every district is one hexagon in a
 * staggered grid. Tile position carries no geographic meaning. Shade steps by value quintile from the data itself.
 */
export function Cartogram({ tiles, ariaLabel, tableCaption, valueLabel, lowLabel = "Lower", highLabel = "Higher", selected, onSelect }: { tiles: CartogramTile[]; ariaLabel: string; tableCaption: string; valueLabel: string; lowLabel?: string; highLabel?: string; selected?: string | null; onSelect?: (code: string) => void }) {
  const [wrapRef, wrapW] = useWidth(520);
  const vals = tiles.map((t) => t.value).filter((v): v is number => v !== null);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const step = (v: number | null) => (v === null ? -1 : Math.min(4, Math.floor(((v - min) / span) * 5)));
  const shades = ["var(--scale-5)", "var(--scale-4)", "var(--scale-3)", "var(--scale-2)", "var(--scale-1)"];
  const ink = ["#1a1245", "#1a1245", "#ffffff", "#ffffff", "#ffffff"];
  const cols = Math.max(1, Math.min(5, tiles.length));
  const hexW = Math.max(54, Math.min(88, (wrapW - 12) / (cols + 0.5)));
  const hexH = hexW * 1.04;
  const rows: CartogramTile[][] = [];
  for (let i = 0; i < tiles.length; i += cols) rows.push(tiles.slice(i, i + cols));
  return (
    <div>
      <div ref={wrapRef} role={onSelect ? "group" : "img"} aria-label={ariaLabel} className="flex flex-col items-center py-1">
        {rows.map((r, ri) => (
          <div key={ri} className="flex" style={{ marginTop: ri ? -hexH * 0.235 : 0, transform: ri % 2 ? `translateX(${hexW / 2}px)` : undefined }}>
            {r.map((t) => {
              const s = step(t.value);
              const on = selected === t.code;
              const inner = (
                <span>
                  <span className="block text-[0.6875rem] leading-4 font-semibold tracking-wide">{t.label ?? t.code}</span>
                  <span className="tnum block text-sm leading-5 font-bold">{t.display}</span>
                </span>
              );
              const style = { width: hexW, height: hexH, clipPath: "polygon(50% 0, 100% 25%, 100% 75%, 50% 100%, 0 75%, 0 25%)", background: s < 0 ? "var(--chart-empty)" : shades[s], color: s < 0 ? "#3b3463" : ink[s] };
              const cls = "grid shrink-0 place-items-center text-center";
              return (
                <div key={t.code} className={`${on ? "z-10 scale-110" : ""}`} style={{ margin: "0 1px", filter: on ? "drop-shadow(0 5px 6px rgb(43 27 102 / 0.5))" : undefined }}>
                  {onSelect ? (
                    <button type="button" onClick={() => onSelect(t.code)} aria-pressed={on} title={`${t.label ?? t.code}: ${t.display}`} className={`${cls} cursor-pointer`} style={style}>
                      {inner}
                    </button>
                  ) : (
                    <div title={`${t.label ?? t.code}: ${t.display}`} className={cls} style={style}>
                      {inner}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
      <div className="mt-3 flex items-center justify-center gap-2 text-xs text-ink-muted">
        <span>{lowLabel}</span>
        {shades.map((c) => (
          <span key={c} aria-hidden="true" className="h-3 w-7 first:rounded-l-full last:rounded-r-full" style={{ background: c }} />
        ))}
        <span>{highLabel}</span>
      </div>
      <ChartTable caption={tableCaption} columns={["District", valueLabel]} rows={tiles.map((t) => [t.label ?? t.code, t.display])} />
    </div>
  );
}

// ---- FunnelStack -----------------------------------------------------------------------------------------------------
const FUNNEL_SHADES = ["#3b2488", "#47299a", "#532fa8", "#5f38b6", "#6b43c3", "#774fcf", "#835bd9"] as const;
export interface FunnelStackRow {
  label: string;
  value: number;
  /** small line under the figure, for example "41.2% of residents" */
  sub?: string;
}

/** Tapered glass funnel (reference "Care Journey"): each stage is a trapezoid from its own width down to the next stage's width. */
export function FunnelStack({ rows, ariaLabel, tableCaption }: { rows: FunnelStackRow[]; ariaLabel: string; tableCaption: string }) {
  const top = rows[0]?.value || 0;
  const w = rows.map((r) => (top > 0 ? Math.max(40, Math.min(100, (r.value / top) * 100)) : 40));
  const bottom = (i: number) => (i < rows.length - 1 ? w[i + 1] : Math.max(34, w[i] * 0.86));
  return (
    <div>
      <ol aria-label={ariaLabel} className="flex flex-col gap-1">
        {rows.map((r, i) => {
          const t = w[i];
          const b = bottom(i);
          return (
            <li
              key={r.label}
              className="relative grid h-[2.85rem] place-items-center text-center text-white"
              style={{
                clipPath: `polygon(${(100 - t) / 2}% 0, ${(100 + t) / 2}% 0, ${(100 + b) / 2}% 100%, ${(100 - b) / 2}% 100%)`,
                background: `linear-gradient(180deg, rgb(255 255 255 / 0.22), rgb(255 255 255 / 0) 55%), ${FUNNEL_SHADES[Math.min(i, FUNNEL_SHADES.length - 1)]}`,
              }}
            >
              <span className="px-2 leading-tight">
                <span className="tnum block font-display text-[1.05rem] leading-5 font-bold">{r.value.toLocaleString("en-US")}</span>
                <span className="block truncate text-[0.625rem] leading-3">
                  {r.label}
                  {r.sub ? `, ${r.sub}` : ""}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
      <ChartTable caption={tableCaption} columns={["Stage", "Count"]} rows={rows.map((r) => [r.label, r.value.toLocaleString("en-US")])} />
    </div>
  );
}

// ---- Columns ---------------------------------------------------------------------------------------------------------
export interface ColumnItem {
  label: string;
  value: number | null;
  display: string;
}

/** Single-series columns with the value written above each column; heights share one zero baseline. */
export function Columns({ items, max, height = 150, ariaLabel, tableCaption, valueHeader = "Value" }: { items: ColumnItem[]; max?: number; height?: number; ariaLabel: string; tableCaption: string; valueHeader?: string }) {
  const top = max ?? Math.max(1e-9, ...items.map((i) => i.value ?? 0));
  return (
    <div>
      <ul aria-label={ariaLabel} className="flex items-end justify-between gap-1.5" style={{ height: height + 44 }}>
        {items.map((it) => (
          <li key={it.label} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1">
            <span className="tnum text-xs font-semibold text-ink">{it.display}</span>
            <span
              aria-hidden="true"
              className="block w-full max-w-12 rounded-t-lg"
              style={{ height: `${it.value === null ? 2 : Math.max(2, (it.value / top) * height)}px`, background: `linear-gradient(180deg, ${CHART_SCALE[Math.min(CHART_SCALE.length - 2, Math.floor((1 - (it.value ?? 0) / top) * (CHART_SCALE.length - 1)))]}, ${CHART.light})` }}
            />
            <span className="w-full truncate text-center text-xs text-ink-muted" title={it.label}>
              {it.label}
            </span>
          </li>
        ))}
      </ul>
      <ChartTable caption={tableCaption} columns={["Group", valueHeader]} rows={items.map((it) => [it.label, it.display])} />
    </div>
  );
}

// ---- Ring ------------------------------------------------------------------------------------------------------------
/** Small circular share (0 to 1) with the figure in the middle and a label under it. `value === null` shows "None". */
export function Ring({ value, label, display, size = 76 }: { value: number | null; label: string; display: string; size?: number }) {
  const r = 30;
  const c = 2 * Math.PI * r;
  const f = value === null ? 0 : Math.max(0, Math.min(1, value));
  return (
    <div className="flex flex-col items-center text-center">
      <div className="relative" style={{ width: size, height: size }}>
        <svg viewBox="0 0 72 72" role="img" aria-label={`${label}: ${value === null ? "not available" : display}`} className="size-full -rotate-90">
          <circle cx="36" cy="36" r={r} fill="none" stroke="var(--chart-track)" strokeWidth="8" />
          {value !== null && <circle cx="36" cy="36" r={r} fill="none" stroke={CHART.tealDark} strokeWidth="8" strokeLinecap="round" strokeDasharray={`${f * c} ${c}`} />}
        </svg>
        <span className="tnum pointer-events-none absolute inset-0 grid place-items-center font-display text-base font-bold text-ink">{value === null ? "None" : display}</span>
      </div>
      <p className="mt-1.5 text-xs leading-snug text-ink-soft">{label}</p>
    </div>
  );
}
