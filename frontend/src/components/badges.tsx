import type { ReactNode } from "react";
import { DataLabel } from "./ui";

/**
 * Owner decision 2026-11-12: the product UI carries no "synthetic" banner, badge or wording; the disclosure lives in the
 * README. Source labels are only Live or Evaluation (DataLabel). `SimulatedBadge` stays ONLY on outreach delivery and
 * booking events, because no real SMS or appointment is ever sent.
 */

/** Label for simulated actions: outreach delivery and bookings only. */
export function SimulatedBadge({ children = "Simulated" }: { children?: ReactNode }) {
  return <DataLabel kind="simulated">{children}</DataLabel>;
}

/** @deprecated Removed from the UI by owner decision; renders nothing. Delete the call site. */
export function SyntheticBadge(props: { children?: ReactNode }) {
  void props;
  return null;
}
