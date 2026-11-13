import { useId } from "react";

/** Logo mark: an "A" formed from two rising strokes with a care dot. Gradient is tokenised through CSS variables. */
export function BrandMark({ className = "size-9" }: { className?: string }) {
  const gid = useId();
  return (
    <svg viewBox="0 0 40 40" aria-hidden="true" className={`shrink-0 ${className}`}>
      <defs>
        <linearGradient id={gid} x1="4" y1="36" x2="34" y2="4" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#4f33a8" />
          <stop offset="1" stopColor="#8d6df0" />
        </linearGradient>
      </defs>
      <path d="M6 35 20 5l14 30" fill="none" stroke={`url(#${gid})`} strokeWidth="5.2" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M12.5 26.5c4.5 1.6 10.5 1.6 15 0" fill="none" stroke={`url(#${gid})`} strokeWidth="4" strokeLinecap="round" />
      <circle cx="31.5" cy="8" r="3.2" fill="#b9a2f7" />
    </svg>
  );
}

/** Logo mark and wordmark. `tone="dark"` is for deep violet surfaces, `"light"` for glass and white surfaces. */
export function Brand({ tone = "light", subtitle = true, mark = true }: { tone?: "dark" | "light"; subtitle?: boolean; mark?: boolean }) {
  return (
    <span className="flex items-center gap-2.5">
      {mark && <BrandMark />}
      <span className="leading-tight">
        <span className={`block font-display text-[1.35rem] font-bold tracking-tight ${tone === "dark" ? "text-white" : "text-ink"}`}>Anticipa</span>
        {subtitle && <span className={`block whitespace-nowrap text-[0.625rem] font-medium ${tone === "dark" ? "text-navy-muted" : "text-ink-soft"}`}>Population Health Intelligence</span>}
      </span>
    </span>
  );
}
