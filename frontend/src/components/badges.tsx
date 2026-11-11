import type { ReactNode } from "react";

/** Persistent notice on every page (CLAUDE.md §4 principle 10; contract §8). */
export function SyntheticBanner() {
  return (
    <div
      role="note"
      className="hatch sticky top-0 z-40 flex h-(--banner-h) items-center justify-center border-b border-specimen bg-specimen-bg px-4 text-sm font-medium text-specimen"
    >
      Synthetic data — not for clinical use
    </div>
  );
}

const base =
  "inline-flex items-center rounded-sm border px-1.5 text-xs font-medium leading-5 whitespace-nowrap text-specimen bg-specimen-bg border-specimen";

/** Label for any value derived from synthetic residents/records. */
export function SyntheticBadge({ children = "Synthetic" }: { children?: ReactNode }) {
  return <span className={`${base} hatch`}>{children}</span>;
}

/** Label for simulated actions (bookings, messages, outcomes). Dashed border distinguishes it. */
export function SimulatedBadge({ children = "Simulated" }: { children?: ReactNode }) {
  return <span className={`${base} border-dashed`}>{children}</span>;
}
