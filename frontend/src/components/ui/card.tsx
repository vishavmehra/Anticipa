import type { ReactNode } from "react";
import { Icon, type IconName } from "./icons";
import { Sparkline } from "./charts";

// ---- surfaces ---------------------------------------------------------------------------------------------------
export function Card({ children, className = "", as: Tag = "div", ...rest }: { children: ReactNode; className?: string; as?: "div" | "section" | "article" } & React.HTMLAttributes<HTMLElement>) {
  return (
    <Tag className={`card ${className}`} {...rest}>
      {children}
    </Tag>
  );
}

/** Title row for a card: optional icon, serif title, optional subtitle and a right-hand slot (link, filter, label). */
export function CardHeader({ title, subtitle, icon, aside, id }: { title: ReactNode; subtitle?: ReactNode; icon?: IconName; aside?: ReactNode; id?: string }) {
  return (
    <div className="flex items-start justify-between gap-3 px-5 pt-4 pb-3">
      <div className="flex min-w-0 items-start gap-3">
        {icon && (
          <span className="grid size-9 shrink-0 place-items-center icon-tile">
            <Icon name={icon} className="size-[1.1rem]" />
          </span>
        )}
        <div className="min-w-0">
          <h2 id={id} className="font-display text-lg leading-6 font-bold tracking-tight">
            {title}
          </h2>
          {subtitle && <p className="text-[0.8125rem] text-ink-muted">{subtitle}</p>}
        </div>
      </div>
      {aside && <div className="flex shrink-0 flex-wrap items-center gap-2">{aside}</div>}
    </div>
  );
}

// ---- pills and labels ----------------------------------------------------------------------------------------------
export type Tone = "ok" | "warn" | "danger" | "info" | "neutral" | "brand";
const PILL: Record<Tone, string> = {
  ok: "bg-ok-bg text-ok",
  warn: "bg-warn-bg text-warn",
  danger: "bg-danger-bg text-danger",
  info: "bg-info-bg text-info",
  neutral: "bg-neutral-bg text-neutral",
  brand: "bg-brand-soft text-brand-ink",
};

export function Pill({ tone = "neutral", icon, children }: { tone?: Tone; icon?: IconName; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs leading-5 font-semibold whitespace-nowrap ${PILL[tone]}`}>
      {icon && <Icon name={icon} className="size-3.5" strokeWidth={2.2} />}
      {children}
    </span>
  );
}

const PRIORITY: Record<string, { label: string; tone: Tone; icon: IconName }> = {
  high: { label: "High", tone: "danger", icon: "alert" },
  elevated: { label: "Medium", tone: "warn", icon: "warning" },
  medium: { label: "Medium", tone: "warn", icon: "warning" },
  routine: { label: "Low", tone: "ok", icon: "check-circle" },
  low: { label: "Low", tone: "ok", icon: "check-circle" },
};

/** Care-gap priority as the backend names it (high | elevated | routine). Text label plus icon, never colour alone. */
export function PriorityPill({ priority }: { priority: string | null | undefined }) {
  if (!priority) return <span className="text-ink-muted">None</span>;
  const p = PRIORITY[priority.toLowerCase()] ?? { label: priority.replace(/_/g, " "), tone: "neutral" as Tone, icon: "info" as IconName };
  return (
    <span data-priority={priority.toLowerCase()}>
      <Pill tone={p.tone} icon={p.icon}>
        {p.label}
      </Pill>
    </span>
  );
}

/**
 * Where a figure comes from. Owner decision 2026-11-12: source labels are only "Live" (counted from the running
 * database now) or "Evaluation" (result of the evaluation harness). "Simulated" is allowed ONLY on outreach delivery and
 * booking events, because no real SMS or appointment is ever sent. There is no "synthetic" label in the UI.
 */
export function DataLabel({ kind, children }: { kind: "live" | "evaluation" | "measured" | "simulated"; children?: ReactNode }) {
  const base = "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs leading-5 font-semibold whitespace-nowrap";
  if (kind === "live")
    return (
      <span className={`${base} border-ok-line bg-ok-bg text-ok`}>
        <span aria-hidden="true" className="size-1.5 rounded-full bg-ok" />
        {children ?? "Live"}
      </span>
    );
  if (kind === "simulated") return <span className={`${base} border-dashed border-warn-line bg-warn-bg text-warn`}>{children ?? "Simulated"}</span>;
  return <span className={`${base} border-info-line bg-info-bg text-info`}>{children ?? "Evaluation"}</span>;
}

// ---- KPI tile -------------------------------------------------------------------------------------------------------
const TINT = {
  teal: "icon-tile icon-tile-teal",
  rose: "icon-tile icon-tile-rose",
  blue: "icon-tile icon-tile-blue",
  amber: "icon-tile icon-tile-amber",
  violet: "icon-tile",
  green: "icon-tile icon-tile-green",
  white: "icon-tile",
} as const;
export type KpiTint = keyof typeof TINT;

/**
 * One measured value with an icon (owner reference card: round icon, label, serif figure, optional delta and sparkline).
 * `value === null` renders an explicit "Not available" so a missing measurement can never be mistaken for zero.
 * `basis` states numerator/denominator/period/source; `delta` is a Pill-sized comparison; `spark` is a real series.
 */
export function KpiTile({
  label,
  value,
  icon,
  tint = "white",
  basis,
  delta,
  badge,
  spark,
  loading = false,
}: {
  label: string;
  value: ReactNode | null;
  icon?: IconName;
  tint?: KpiTint;
  basis?: ReactNode;
  delta?: ReactNode;
  badge?: ReactNode;
  spark?: number[];
  loading?: boolean;
}) {
  return (
    <div className="card flex min-h-[8.75rem] flex-col justify-between gap-2 px-5 py-4">
      <div className="flex items-start gap-3.5">
        {icon && (
          <span className={`size-12 ${TINT[tint]}`}>
            <Icon name={icon} className="size-[1.3rem]" />
          </span>
        )}
        <div className="min-w-0 flex-1">
          <p className="text-[0.8125rem] leading-snug font-medium text-ink-soft">{label}</p>
          <div className="mt-0.5 flex items-center justify-between gap-2">
            {loading ? (
              <div className="skeleton my-1 h-8 w-24" role="status" aria-label={`Loading ${label}`} />
            ) : (
              <p className={`tnum min-w-0 truncate ${value === null ? "text-lg font-semibold text-ink-muted" : "font-display text-[1.85rem] leading-9 font-bold tracking-tight"}`}>
                {value === null ? "Not available" : value}
              </p>
            )}
            {badge && <span className="shrink-0">{badge}</span>}
          </div>
        </div>
      </div>
      <div className="min-h-[1.9rem]">
        {spark && spark.length > 1 && <Sparkline values={spark} fluid width={240} height={30} />}
        {(delta || basis) && (
          <div className={spark && spark.length > 1 ? "mt-1.5" : ""}>
            {delta}
            {basis && <p className="text-xs leading-snug text-ink-muted">{basis}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
