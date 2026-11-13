import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { supabaseEnv } from "@/lib/supabase/env";

/**
 * Next.js 16 `proxy` (formerly middleware). Two jobs:
 *  1. Refresh the Supabase session once per navigation and write rotated cookies back (@supabase/ssr pattern).
 *  2. Optimistic UX gate: no auth cookie at all means go to /login. This is not security; FastAPI verifies
 *     the bearer token on every call. Claims verification failures are deliberately not treated as logout here.
 */
export async function proxy(request: NextRequest) {
  const { url, key } = supabaseEnv();
  let response = NextResponse.next({ request });

  const supabase = createServerClient(url, key, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll(cookiesToSet, headers) {
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        cookiesToSet.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
        Object.entries(headers).forEach(([k, v]) => response.headers.set(k, v));
      },
    },
  });

  const hasSession = request.cookies.getAll().some((c) => /^sb-.+-auth-token/.test(c.name));
  if (hasSession) {
    // Triggers the refresh. A network/verification error leaves the cookies as they are.
    await supabase.auth.getClaims().catch(() => undefined);
  } else if (request.nextUrl.pathname !== "/login" && !request.nextUrl.pathname.startsWith("/dev/") && !request.nextUrl.pathname.startsWith("/auth/")) {
    const login = request.nextUrl.clone();
    login.pathname = "/login";
    login.search = `?next=${encodeURIComponent(request.nextUrl.pathname + request.nextUrl.search)}`;
    return NextResponse.redirect(login);
  }
  return response;
}

export const config = {
  // Skip the backend rewrite (/api), Next internals and static assets.
  matcher: ["/((?!api/|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)"],
};
