"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useContext, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { NAV, ROLE_LABELS } from "@/lib/nav";
import { createClient } from "@/lib/supabase/client";
import type { CurrentUser } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { ErrorPanel, btnOnDark, btnSecondary } from "./ui";

const UserContext = createContext<CurrentUser | null>(null);
export const useUser = () => useContext(UserContext);

/** Permission check against what GET /auth/me returned; never against the role name (ADR-060). */
export function useCan(): (permission: string) => boolean {
  const user = useUser();
  return (permission) => !!user?.permissions?.includes(permission);
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

export function AppShell({ children }: { children: ReactNode }) {
  const me = useApi("auth/me", (signal) => api<CurrentUser>("/auth/me", { signal }));
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  if (me.error) {
    // 401 is already redirecting to /login (api.ts); everything else is shown, not swallowed.
    return (
      <div className="mx-auto max-w-xl p-6">
        {me.error.status === 401 ? (
          <p role="status">Redirecting to sign in…</p>
        ) : me.error.code === "FORBIDDEN" ? (
          <div role="alert" className="rounded-md border border-warn bg-warn-bg px-4 py-3 text-warn">
            <h1 className="font-semibold">No Anticipa role assigned — use the demo account provided by the team</h1>
            <p className="mt-0.5">
              You are signed in, but this account has no role in Anticipa, so no data can be shown. Sign out and sign
              in with the demo account.
            </p>
            <button type="button" className={`${btnSecondary} mt-3`} onClick={signOut}>
              Sign out
            </button>
          </div>
        ) : (
          <ErrorPanel error={me.error} onRetry={me.reload} title="Could not verify your session" />
        )}
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
  const items = NAV.filter((n) => n.anyOf.some((p) => user.permissions?.includes(p)));

  return (
    <UserContext.Provider value={user}>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-10 focus:left-2 focus:z-50 focus:rounded-sm focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <div className="md:grid md:grid-cols-[14.5rem_minmax(0,1fr)]">
        <aside className="on-spruce flex flex-col bg-spruce text-[#c9d8dd] md:sticky md:top-(--banner-h) md:h-[calc(100dvh-var(--banner-h))] md:overflow-y-auto">
          <div className="flex items-center justify-between px-4 py-3">
            <Link href="/" className="text-lg font-semibold tracking-tight text-white">
              Anticipa
            </Link>
            <button
              type="button"
              className="min-h-9 rounded-sm border border-[#4d6b76] px-3 text-sm font-medium text-white md:hidden"
              aria-expanded={menuOpen}
              aria-controls="primary-nav"
              onClick={() => setMenuOpen((o) => !o)}
            >
              {menuOpen ? "Close menu" : "Menu"}
            </button>
          </div>
          <div id="primary-nav" className={`${menuOpen ? "flex" : "hidden"} flex-1 flex-col md:flex`}>
            <nav aria-label="Primary" className="flex-1 px-2 pb-3">
              <ul className="flex flex-col gap-0.5">
                {items.map((n) => {
                  const active = n.href === "/" ? pathname === "/" : pathname === n.href || pathname.startsWith(`${n.href}/`);
                  return (
                    <li key={n.href}>
                      <Link
                        href={n.href}
                        aria-current={active ? "page" : undefined}
                        onClick={() => setMenuOpen(false)}
                        className={`block rounded-sm border-l-[3px] px-3 py-1.5 ${
                          active
                            ? "border-white bg-spruce-hover font-medium text-white"
                            : "border-transparent hover:bg-spruce-hover hover:text-white"
                        }`}
                      >
                        {n.label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </nav>
            <div className="border-t border-[#2c4b57] px-4 py-3">
              <p className="font-medium break-all text-white">{user.display_name || user.email || "Signed in"}</p>
              <p className="text-xs text-[#9db4bc]">{ROLE_LABELS[user.role] ?? user.role}</p>
              <button
                type="button"
                onClick={() => {
                  setSigningOut(true);
                  void signOut();
                }}
                disabled={signingOut}
                className={`${btnOnDark} mt-2 w-full`}
              >
                {signingOut ? "Signing out…" : "Sign out"}
              </button>
            </div>
          </div>
        </aside>
        <main id="main" tabIndex={-1} className="min-w-0 px-4 py-6 md:px-8">
          {children}
        </main>
      </div>
    </UserContext.Provider>
  );
}
