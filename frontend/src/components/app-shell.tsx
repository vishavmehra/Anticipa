"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { NAV, ROLE_LABELS, SYSTEM_NAV, SYSTEM_PERMISSION, WRITE_PERMISSIONS, isSystemPath } from "@/lib/nav";
import { createClient } from "@/lib/supabase/client";
import type { AlertsSummary, CurrentUser, ScopeMeta } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { Brand, BrandMark } from "./brand";
import { AskAnticipa } from "./ask";
import { CommandPalette } from "./command-palette";
import { ErrorPanel, Icon, Pill, btnPrimary, btnSecondary, type IconName } from "./ui";

const UserContext = createContext<CurrentUser | null>(null);
export const useUser = () => useContext(UserContext);

/** Permission check against what GET /auth/me returned; never against the role name (ADR-060). */
export function useCan(): (permission: string) => boolean {
  const user = useUser();
  return (permission) => !!user?.permissions?.includes(permission);
}

/** Reporting window chosen in the top bar. Pages that accept a `days` window read it from here. */
export const RANGES = [
  { days: 7, label: "Last 7 days" },
  { days: 14, label: "Last 14 days" },
  { days: 30, label: "Last 30 days" },
  { days: 60, label: "Last 60 days" },
  { days: 90, label: "Last 90 days" },
] as const;
const RangeContext = createContext<{ days: number; setDays: (d: number) => void }>({ days: 30, setDays: () => undefined });
export const useRange = () => useContext(RangeContext);

/** District scope chosen in the top bar. Pages that know their districts register them; selecting one drills the page down to it. */
const ScopeContext = createContext<{ scope: string; setScope: (s: string) => void; districts: readonly string[]; setDistricts: (d: readonly string[]) => void }>({
  scope: "",
  setScope: () => undefined,
  districts: [],
  setDistricts: () => undefined,
});
export const useScope = () => useContext(ScopeContext);

/** Theme modes. Stored in localStorage (guarded) and mirrored on <html data-theme> by an inline script before first paint. */
export type ThemeMode = "light" | "dark" | "clinical";
const THEMES: readonly { id: ThemeMode; label: string; icon: IconName }[] = [
  { id: "light", label: "Light", icon: "theme-light" },
  { id: "dark", label: "Dark", icon: "theme-dark" },
  { id: "clinical", label: "Clinical high contrast", icon: "theme-clinical" },
];
const THEME_KEY = "anticipa-theme";
function readTheme(): ThemeMode {
  if (typeof window === "undefined") return "light";
  try {
    const t = window.localStorage.getItem(THEME_KEY);
    return t === "dark" || t === "clinical" ? t : "light";
  } catch {
    return "light";
  }
}

/** Bell with the live count of active alerts (GET /analytics/alerts/summary, cached for a few seconds by the backend). */
function AlertBell() {
  const res = useApi("alerts-summary", (signal) => api<AlertsSummary>("/analytics/alerts/summary", { signal }));
  const n = res.data?.data.alerts_active ?? 0;
  return (
    <Link href="/#alerts" aria-label={n > 0 ? `${n} active alerts, open Command Center alerts` : "Alerts on Command Center"} className="glass-strong relative grid size-11 place-items-center rounded-full! text-ink-soft hover:text-brand-ink">
      <Icon name="bell" className="size-5" />
      {n > 0 && (
        <span aria-hidden="true" className="absolute -top-1 -right-1 grid h-5 min-w-5 place-items-center rounded-full bg-[#b42318] px-1 text-[0.6875rem] font-bold text-white">
          {n}
        </span>
      )}
    </Link>
  );
}

/** District scope: options come from GET /meta/scope (districts grouped by state). Selecting one drills the page down to it. */
function ScopePill({ scope, setScope }: { scope: string; setScope: (s: string) => void }) {
  const res = useApi("meta-scope", (signal) => api<ScopeMeta>("/meta/scope", { signal }));
  const d = res.data?.data;
  return (
    <div className="glass-strong relative hidden min-h-11 items-center rounded-full! sm:flex">
      <label htmlFor="scope" className="sr-only">
        District scope
      </label>
      <Icon name="pin" className="pointer-events-none absolute left-3.5 size-[1.05rem] text-ink-soft" />
      <select id="scope" value={scope} onChange={(e) => setScope(e.target.value)} className="select-arrow min-h-11 max-w-[11rem] cursor-pointer rounded-full bg-transparent pr-9 pl-10 text-sm font-semibold text-ink">
        <option value="">All districts</option>
        {(d?.states ?? []).map((st) => (
          <optgroup key={st.code} label={st.name}>
            {(d?.districts ?? []).filter((x) => x.state_code === st.code).map((x) => (
              <option key={x.code} value={x.code}>
                {x.name}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}

async function signOut() {
  try {
    await createClient().auth.signOut();
  } catch {
    /* session may already be gone; leave either way */
  }
  // Hard navigation on purpose: drops all signed-in client state and the router cache.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign("/login");
}

/** Signed in, but the account has no Anticipa role (403 from /auth/me). Shows no data. */
function AccessPending({ onRetry }: { onRetry: () => void }) {
  return (
    <main className="grid min-h-[calc(100dvh-var(--banner-h))] place-items-center px-4 py-10">
      <div role="alert" className="card w-full max-w-lg p-8 text-center">
        <span className="mx-auto grid size-12 place-items-center icon-tile icon-tile-warn">
          <Icon name="lock" className="size-6" />
        </span>
        <h1 className="mt-4 font-display text-2xl font-bold tracking-tight">Access pending. Ask an administrator.</h1>
        <p className="mx-auto mt-2 max-w-[44ch] text-ink-muted">
          You are signed in, but no Anticipa role is assigned to this account yet, so no data can be shown. An administrator can grant access, or you can sign in with
          a different account.
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          <button type="button" className={btnPrimary} onClick={onRetry}>
            Check again
          </button>
          <button type="button" className={btnSecondary} onClick={signOut}>
            Sign out
          </button>
        </div>
      </div>
    </main>
  );
}

/** Shown instead of a page the signed-in account may not open. Nothing is fetched for it. */
function NoAccess() {
  return (
    <div role="status" className="card mx-auto mt-10 max-w-lg p-8 text-center">
      <span className="mx-auto grid size-12 place-items-center icon-tile icon-tile-neutral">
        <Icon name="lock" className="size-6" />
      </span>
      <h1 className="mt-4 font-display text-2xl font-bold tracking-tight">You do not have access to this page</h1>
      <p className="mx-auto mt-2 max-w-[42ch] text-ink-muted">This page is limited to system administrators. Ask an administrator if you need it.</p>
      <Link href="/" className={`${btnPrimary} mt-6`}>
        Back to Command Center
      </Link>
    </div>
  );
}

const initials = (s: string) =>
  s
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase())
    .join("") || "A";

const navLink = (on: boolean) =>
  `flex min-h-11 items-center gap-3 rounded-[0.95rem] px-3.5 py-2.5 text-[0.875rem] whitespace-nowrap transition-colors ${
    on ? "grad-brand font-semibold text-white shadow-[0_8px_16px_-8px_rgb(79_51_168/0.8)]" : "font-medium text-ink-soft hover:bg-surface/80 hover:text-ink"
  }`;
/** Labels collapse into tooltips on the desktop rail and open when the rail is hovered or holds focus. */
const railLabel = "md:opacity-0 md:transition-opacity md:group-hover/rail:opacity-100 md:group-focus-within/rail:opacity-100";

export function AppShell({ children }: { children: ReactNode }) {
  const me = useApi("auth/me", (signal) => api<CurrentUser>("/auth/me", { signal }));
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const [avatarOpen, setAvatarOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [days, setDays] = useState(30);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>(readTheme);
  const [scope, setScope] = useState("");
  const [districts, setDistricts] = useState<readonly string[]>([]);
  const avatarRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* storage can be blocked; the choice then lasts for this visit only */
    }
  }, [theme]);

  // Command or Control + K focuses search; Escape and outside clicks close the account menu.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
      }
      if (e.key === "Escape") setAvatarOpen(false);
    };
    const onDown = (e: MouseEvent) => {
      if (avatarRef.current && !avatarRef.current.contains(e.target as Node)) setAvatarOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, []);

  if (me.error) {
    // 401 is already redirecting to /login (api.ts); everything else is shown, not swallowed.
    if (me.error.status === 401) {
      return (
        <p role="status" className="p-6">
          Redirecting to sign in…
        </p>
      );
    }
    if (me.error.code === "FORBIDDEN") return <AccessPending onRetry={me.reload} />;
    return (
      <div className="mx-auto max-w-xl p-6">
        <ErrorPanel error={me.error} onRetry={me.reload} title="Could not verify your session" />
      </div>
    );
  }
  if (!me.data) {
    return (
      <p role="status" className="p-6 text-ink-muted">
        Checking your session…
      </p>
    );
  }

  const user = me.data.data;
  const can = (p: string) => !!user.permissions?.includes(p);
  const items = NAV.filter((n) => n.anyOf.some(can));
  const isSystem = can(SYSTEM_PERMISSION);
  const systemItems = isSystem ? SYSTEM_NAV : [];
  const blocked = !isSystem && isSystemPath(pathname);
  const readOnly = !WRITE_PERMISSIONS.some(can);
  // The longest matching nav entry wins, so /guidelines does not light up on /guidelines/studio.
  const matches = (href: string) => (href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`));
  const activeHref = [...items, ...systemItems].filter((n) => matches(n.href)).sort((a, b) => b.href.length - a.href.length)[0]?.href;
  const active = (href: string) => href === activeHref;
  const name = user.display_name || user.email || "Signed in";
  const canSeeAlerts = can("analytics.view") || can("analytics.view_limited");


  return (
    <UserContext.Provider value={user}>
      <RangeContext.Provider value={{ days, setDays }}>
      <ScopeContext.Provider value={{ scope, setScope, districts, setDistricts }}>
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded-control focus:bg-surface focus:px-3 focus:py-2 focus:shadow-pop"
        >
          Skip to content
        </a>
        <AskAnticipa variant="launcher" />
        <CommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
          pages={[...items, ...systemItems]}
          canResidents={can("residents.view")}
          actions={[
            ...(canSeeAlerts ? [{ id: "report", label: "Generate a report", icon: "file" as IconName, href: "/reports" }] : []),
            ...THEMES.filter((t) => t.id !== theme).map((t) => ({ id: `theme-${t.id}`, label: `Switch to ${t.label.toLowerCase()} theme`, icon: t.icon, run: () => setTheme(t.id) })),
          ]}
        />
        <div className="md:grid md:grid-cols-[var(--sidebar-w)_minmax(0,1fr)]">
          <aside className="md:relative">
            <div className="glass-side group/rail flex flex-col md:fixed md:inset-y-0 md:left-0 md:z-40 md:w-[var(--sidebar-w)] md:overflow-hidden md:transition-[width,box-shadow] md:duration-200 md:hover:w-[14.25rem] md:hover:shadow-pop md:focus-within:w-[14.25rem] md:focus-within:shadow-pop">
              <div className="flex items-center justify-between px-4 py-3 md:px-3.5 md:pt-4 md:pb-3">
                <Link href="/" className="rounded-control">
                  <span className="md:hidden">
                    <Brand />
                  </span>
                  <span className="hidden md:block">
                    <BrandMark className="size-11" />
                    <span className="sr-only">Anticipa home</span>
                  </span>
                </Link>
                <button
                  type="button"
                  className="inline-flex min-h-10 items-center gap-2 rounded-full border border-line-strong px-3.5 text-sm font-semibold text-ink md:hidden"
                  aria-expanded={menuOpen}
                  aria-controls="primary-nav"
                  onClick={() => setMenuOpen((o) => !o)}
                >
                  <Icon name={menuOpen ? "close" : "menu"} className="size-4" />
                  {menuOpen ? "Close menu" : "Menu"}
                </button>
              </div>
              <div id="primary-nav" className={`${menuOpen ? "flex" : "hidden"} min-h-0 flex-1 flex-col md:flex`}>
                <nav aria-label="Primary" className="scroll-quiet flex-1 overflow-y-auto overflow-x-hidden px-2.5 pt-1 pb-3">
                  <ul className="flex flex-col gap-1">
                    {items.map((n) => (
                      <li key={n.href}>
                        <Link href={n.href} title={n.label} aria-current={active(n.href) ? "page" : undefined} onClick={() => setMenuOpen(false)} className={navLink(active(n.href))}>
                          <Icon name={n.icon} className="size-[1.2rem]" />
                          <span className={railLabel}>{n.label}</span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                </nav>
                {systemItems.length > 0 && (
                  <details className="group mx-2.5 mb-4" open={isSystemPath(pathname)}>
                    <summary title="System" className="flex min-h-11 cursor-pointer list-none items-center gap-3 rounded-[0.95rem] px-3.5 py-2.5 text-[0.875rem] font-medium text-ink-soft hover:bg-surface/80 hover:text-ink [&::-webkit-details-marker]:hidden">
                      <Icon name="settings" className="size-[1.2rem]" />
                      <span className={railLabel}>System</span>
                      <Icon name="chevron-down" className={`ml-auto size-4 transition-transform group-open:rotate-180 ${railLabel}`} />
                    </summary>
                    <ul className="mt-1 flex flex-col gap-1">
                      {systemItems.map((n) => (
                        <li key={n.href}>
                          <Link href={n.href} title={n.label} aria-current={active(n.href) ? "page" : undefined} onClick={() => setMenuOpen(false)} className={navLink(active(n.href))}>
                            <Icon name={n.icon as IconName} className="size-[1.2rem]" />
                            <span className={railLabel}>{n.label}</span>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            </div>
          </aside>

          <div className="min-w-0">
            <header className="glass-bar sticky top-0 z-30 flex min-h-[var(--topbar-h)] flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 md:px-8">
              <Link href="/" className="hidden shrink-0 rounded-control md:block" tabIndex={-1}>
                <Brand mark={false} />
              </Link>
              <button
                type="button"
                onClick={() => setPaletteOpen(true)}
                aria-haspopup="dialog"
                className="glass-strong relative flex min-h-11 min-w-0 flex-1 basis-56 items-center gap-3 rounded-full! pr-3 pl-4 text-left text-sm text-ink-muted hover:text-ink md:ml-4 md:max-w-md"
              >
                <Icon name="search" className="size-[1.05rem] shrink-0" />
                <span className="min-w-0 flex-1 truncate">Search pages, actions or residents</span>
                <kbd className="hidden rounded-md bg-brand-soft px-2 py-0.5 font-sans text-xs font-semibold text-brand-ink sm:block">⌘ K</kbd>
              </button>
              <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2.5">
                {readOnly && (
                  <span className="hidden md:inline-flex">
                    <Pill tone="info" icon="eye">
                      Read only access
                    </Pill>
                  </span>
                )}
                <ScopePill scope={scope} setScope={setScope} />
                <div className="glass-strong relative flex min-h-11 items-center rounded-full!">
                  <label htmlFor="range" className="sr-only">
                    Reporting window
                  </label>
                  <Icon name="calendar" className="pointer-events-none absolute left-3.5 size-[1.05rem] text-ink-soft" />
                  <select
                    id="range"
                    value={days}
                    onChange={(e) => setDays(Number(e.target.value))}
                    className="select-arrow min-h-11 cursor-pointer rounded-full bg-transparent pr-9 pl-10 text-sm font-semibold text-ink"
                  >
                    {RANGES.map((r) => (
                      <option key={r.days} value={r.days}>
                        {r.label}
                      </option>
                    ))}
                  </select>
                </div>
                {canSeeAlerts && <AlertBell />}
                <div ref={avatarRef} className="relative">
                  <button
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={avatarOpen}
                    onClick={() => setAvatarOpen((o) => !o)}
                    className="grad-brand grid size-11 place-items-center rounded-full text-sm font-bold shadow-[0_8px_16px_-8px_rgb(79_51_168/0.8)]"
                  >
                    <span className="sr-only">Account menu, </span>
                    {initials(name)}
                  </button>
                  {avatarOpen && (
                    <div role="menu" aria-label="Account" className="glass-pop absolute right-0 z-40 mt-2 w-64 p-3">
                      <p className="truncate px-1 text-sm font-semibold text-ink">{name}</p>
                      <p className="truncate px-1 text-xs text-ink-muted">{ROLE_LABELS[user.role] ?? user.role}</p>
                      <div role="group" aria-label="Theme" className="mt-3 border-t border-line pt-3">
                        <p className="px-1 pb-1.5 text-xs font-semibold text-ink-muted">Theme</p>
                        <div className="flex gap-1.5">
                          {THEMES.map((t) => (
                            <button
                              key={t.id}
                              type="button"
                              role="menuitemradio"
                              aria-checked={theme === t.id}
                              aria-label={`${t.label} theme`}
                              title={`${t.label} theme`}
                              onClick={() => setTheme(t.id)}
                              className={`grid size-10 flex-1 place-items-center rounded-control transition-colors ${theme === t.id ? "grad-brand text-white" : "bg-brand-soft/60 text-ink-soft hover:bg-brand-soft hover:text-brand-ink"}`}
                            >
                              <Icon name={t.icon} className="size-[1.1rem]" />
                            </button>
                          ))}
                        </div>
                      </div>
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setSigningOut(true);
                          void signOut();
                        }}
                        disabled={signingOut}
                        className={`${btnSecondary} mt-3 w-full`}
                      >
                        <Icon name="logout" className="size-4" />
                        {signingOut ? "Signing out…" : "Sign out"}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </header>
            {readOnly && (
              <div className="border-b border-line bg-info-bg px-4 py-2 text-[0.8125rem] font-medium text-info md:hidden">
                <Icon name="eye" className="mr-1.5 inline size-3.5 align-text-bottom" />
                Read only access
              </div>
            )}
            <main id="main" tabIndex={-1} className="mx-auto max-w-[1560px] min-w-0 px-4 pt-6 pb-24 md:pt-7 md:pr-24 md:pb-24 md:pl-8">
              {blocked ? <NoAccess /> : children}
            </main>
          </div>
        </div>
      </ScopeContext.Provider>
      </RangeContext.Provider>
    </UserContext.Provider>
  );
}
