import type { ReactNode } from "react";
import { fmtDateTime } from "@/lib/format";

export interface TimelineItem {
  id: string;
  /** ISO 8601 UTC; rendered as UTC. */
  at: string | null | undefined;
  title: ReactNode;
  detail?: ReactNode;
  tone?: "ok" | "danger" | "info" | "neutral";
}

const DOT = {
  ok: "bg-ok",
  danger: "bg-danger",
  info: "bg-info",
  neutral: "bg-line-strong",
} as const;

/** Chronological list. Order is the caller's; the markup is an ordered list, which is correct for a sequence. */
export function Timeline({ items, label }: { items: readonly TimelineItem[]; label: string }) {
  return (
    <ol aria-label={label} className="relative ml-1.5 border-l border-line-strong">
      {items.map((it) => (
        <li key={it.id} className="relative pb-4 pl-5 last:pb-0">
          <span
            aria-hidden="true"
            className={`absolute top-1.5 -left-[5px] size-[9px] rounded-full ring-2 ring-surface ${DOT[it.tone ?? "neutral"]}`}
          />
          <p className="font-medium">{it.title}</p>
          <p className="tnum text-xs text-ink-muted">
            <time dateTime={it.at ?? undefined}>{fmtDateTime(it.at)}</time>
          </p>
          {it.detail && <p className="mt-0.5 text-ink-muted">{it.detail}</p>}
        </li>
      ))}
    </ol>
  );
}
