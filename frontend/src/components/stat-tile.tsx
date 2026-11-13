import type { ReactNode } from "react";
import { KpiTile, type IconName, type KpiTint } from "./ui";

/**
 * One measured value. `value === null` renders an explicit "Not available" so a missing measurement can never be
 * mistaken for zero. Every metric states its basis (numerator / denominator / period / source).
 * Thin wrapper over KpiTile so older call sites keep working; pass `icon` and `tint` for the premium tile look.
 */
export function StatTile({
  label,
  value,
  basis,
  badge,
  loading = false,
  icon,
  tint,
  delta,
}: {
  label: string;
  value: ReactNode | null;
  loading?: boolean;
  basis?: ReactNode;
  badge?: ReactNode;
  icon?: IconName;
  tint?: KpiTint;
  delta?: ReactNode;
}) {
  return <KpiTile label={label} value={value} loading={loading} basis={basis} badge={badge} icon={icon} tint={tint} delta={delta} />;
}
