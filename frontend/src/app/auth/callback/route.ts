import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

/**
 * OAuth return point (Google). Supabase redirects here with `?code=`; we exchange it for a session (cookies are written
 * by the @supabase/ssr server client) and land on the Command Center. Role comes from the backend (GET /auth/me), never
 * from this route. Anything unexpected goes back to /login with an error flag.
 */
export async function GET(request: Request) {
  const { searchParams, origin } = new URL(request.url);
  // Behind a proxy (Vercel) the request URL can carry an internal host; prefer the forwarded public host.
  const forwardedHost = request.headers.get("x-forwarded-host");
  const base = process.env.NODE_ENV === "development" || !forwardedHost ? origin : `https://${forwardedHost}`;

  const code = searchParams.get("code");
  if (code && !searchParams.get("error")) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) return NextResponse.redirect(`${base}/`);
  }
  return NextResponse.redirect(`${base}/login?error=oauth`);
}
