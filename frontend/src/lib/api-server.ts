import { API_BASE, ApiError, send, toQuery, type Envelope, type RequestOptions } from "./api";
import { createClient } from "./supabase/server";

/**
 * Server-side counterpart of `api()` for Server Components / Route Handlers: reads the Supabase session
 * from cookies, calls FastAPI directly at ANTICIPA_API_ORIGIN with the same Bearer header.
 * The backend verifies the token, so reading it with getSession() here is safe; no 401 refresh loop
 * (proxy.ts refreshes on navigation). Not used by the current pages, which fetch in the browser.
 */
export async function serverApi<T>(path: string, opts: RequestOptions = {}): Promise<Envelope<T>> {
  const origin = process.env.ANTICIPA_API_ORIGIN ?? "http://localhost:8000";
  const supabase = await createClient();
  const { data } = await supabase.auth.getSession();
  if (!data.session) throw new ApiError("UNAUTHENTICATED", "No active session.", 401);
  return send<T>(`${origin}${API_BASE}${path}${toQuery(opts.query)}`, data.session.access_token, opts);
}
